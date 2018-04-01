# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Sequence-to-sequence model with an attention mechanism."""
import pdb
import random

import numpy as np
import tensorflow as tf
from tensorflow.python.framework import ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import variable_scope

import seq2seq_helper
import utils.data_utils as data_utils


class Seq2SeqModel(object):
    """Sequence-to-sequence model with attention and for multiple buckets.

    This class implements a multi-layer recurrent neural network as encoder,
    and an attention-based decoder. This is the same as the model described in
    this paper: http://arxiv.org/abs/1412.7449 - please look there for details,
    or into the seq2seq library for complete model implementation.
    This class also allows to use GRU cells in addition to LSTM cells, and
    sampled softmax to handle large output vocabulary size. A single-layer
    version of this model, but with bi-directional encoder, was presented in
      http://arxiv.org/abs/1409.0473
    and sampled softmax is described in Section 3 of the following paper.
      http://arxiv.org/abs/1412.2007
    """

    def __init__(self,
                 source_vocab_size,
                 target_vocab_size,
                 buckets,
                 size,
                 num_layers,
                 latent_dim,
                 max_gradient_norm,
                 batch_size,
                 learning_rate,
                 kl_min=2,
                 word_dropout_keep_prob=1.0,
                 anneal=False,
                 kl_rate_rise_factor=None,
                 use_lstm=False,
                 num_samples=512,
                 optimizer=None,
                 activation=tf.nn.relu,
                 forward_only=False,
                 feed_previous=True,
                 bidirectional=False,
                 weight_initializer=None,
                 bias_initializer=None,
                 iaf=False,
                 dtype=tf.float32):
        """Create the model.

        Args:
          source_vocab_size: size of the source vocabulary.
          target_vocab_size: size of the target vocabulary.
          buckets: a list of pairs (I, O), where I specifies maximum input length
            that will be processed in that bucket, and O specifies maximum output
            length. Training instances that have inputs longer than I or outputs
            longer than O will be pushed to the next bucket and padded accordingly.
            We assume that the list is sorted, e.g., [(2, 4), (8, 16)].
          size: number of units in each layer of the model.
          num_layers: number of layers in the model.
          max_gradient_norm: gradients will be clipped to maximally this norm.
          batch_size: the size of the batches used during training;
            the model construction is independent of batch_size, so it can be
            changed after initialization if this is convenient, e.g., for decoding.
          learning_rate: learning rate to start with.
          use_lstm: if true, we use LSTM cells instead of GRU cells.
          num_samples: number of samples for sampled softmax.
          forward_only: if set, we do not construct the backward pass in the model.
          dtype: the data type to use to store internal variables.
        """
        self.source_vocab_size = source_vocab_size
        self.target_vocab_size = target_vocab_size
        self.latent_dim = latent_dim
        self.buckets = buckets
        self.batch_size = batch_size
        self.word_dropout_keep_prob = word_dropout_keep_prob
        self.kl_min = kl_min
        feed_previous = feed_previous or forward_only

        self.learning_rate = tf.Variable(
            float(learning_rate), trainable=False, dtype=dtype)

        self.enc_embedding = tf.get_variable("enc_embedding", [source_vocab_size, size], dtype=dtype, trainable=False)
        self.dec_embedding = tf.get_variable("dec_embedding", [target_vocab_size, size], dtype=dtype, trainable=False)

        self.kl_rate = tf.Variable(
            0.0, trainable=False, dtype=dtype)
        self.new_kl_rate = tf.placeholder(tf.float32, shape=[], name="new_kl_rate")
        self.kl_rate_update = tf.assign(self.kl_rate, self.new_kl_rate)

        self.replace_input = tf.placeholder(tf.int32, shape=[None], name="replace_input")
        replace_input = tf.nn.embedding_lookup(self.dec_embedding, self.replace_input)

        self.global_step = tf.Variable(0, trainable=False)

        # If we use sampled softmax, we need an output projection.
        output_projection = None
        softmax_loss_function = None
        # Sampled softmax only makes sense if we sample less than vocabulary size.
        if num_samples > 0 and num_samples < self.target_vocab_size:
            self.w_t = tf.get_variable("proj_w", [self.target_vocab_size, size], dtype=dtype,
                                       initializer=weight_initializer())
            self.w = tf.transpose(self.w_t)
            self.b = tf.get_variable("proj_b", [self.target_vocab_size], dtype=dtype, initializer=bias_initializer)
            output_projection = (self.w, self.b)

            softmax_loss_function = self.sampled_loss
        # Create the internal multi-layer state for our RNN.

        # Create the internal multi-layer state for our RNN.
        if use_lstm:
            if num_layers > 1:
                state = tf.contrib.rnn.MultiRNNCell([tf.contrib.rnn.BasicLSTMCell(size) for _ in range(num_layers)])
            else:
                state = tf.contrib.rnn.BasicLSTMCell(size)
        else:
            if num_layers > 1:
                state = tf.contrib.rnn.MultiRNNCell([tf.contrib.rnn.GRUCell(size) for _ in range(num_layers)])
            else:
                state = tf.contrib.rnn.GRUCell(size)

        def encoder_f(encoder_inputs):
            return seq2seq_helper.embedding_encoder(
                encoder_inputs,
                state,
                self.enc_embedding,
                num_symbols=source_vocab_size,
                embedding_size=size,
                bidirectional=bidirectional,
                weight_initializer=weight_initializer,
                dtype=dtype)

        def decoder_f(encoder_state, decoder_inputs):
            return seq2seq_helper.embedding_rnn_decoder(
                decoder_inputs,
                encoder_state,
                state,
                embedding=self.dec_embedding,
                word_dropout_keep_prob=word_dropout_keep_prob,
                replace_input=replace_input,
                num_symbols=target_vocab_size,
                embedding_size=size,
                output_projection=output_projection,
                feed_previous=feed_previous,
                weight_initializer=weight_initializer)

        def enc_latent_f(encoder_state):
            return seq2seq_helper.encoder_to_latent(
                encoder_state,
                embedding_size=size,
                latent_dim=latent_dim,
                num_layers=num_layers,
                activation=activation,
                use_lstm=use_lstm,
                enc_state_bidirectional=bidirectional,
                dtype=dtype)

        def latent_dec_f(latent_vector):
            return seq2seq_helper.latent_to_decoder(latent_vector,
                                                    embedding_size=size,
                                                    latent_dim=latent_dim,
                                                    num_layers=num_layers,
                                                    activation=activation,
                                                    use_lstm=use_lstm,
                                                    dtype=dtype)

        def sample_f(mean, logvar):
            return seq2seq_helper.sample(
                mean,
                logvar,
                latent_dim,
                iaf,
                kl_min,
                anneal,
                self.kl_rate,
                dtype)

        # Feeds for inputs.
        self.encoder_inputs = []
        self.decoder_inputs = []
        self.target_weights = []
        for i in range(buckets[-1][0]):  # Last bucket is the biggest one.
            self.encoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="encoder{0}".format(i)))
        for i in range(buckets[-1][1] + 1):
            self.decoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="decoder{0}".format(i)))
            self.target_weights.append(tf.placeholder(dtype, shape=[None],
                                                      name="weight{0}".format(i)))

        # Our targets are decoder inputs shifted by one.
        targets = [self.decoder_inputs[i + 1]
                   for i in range(len(self.decoder_inputs) - 1)]

        self.means, self.logvars = seq2seq_helper.variational_encoder_with_buckets(
            self.encoder_inputs, buckets, encoder_f, enc_latent_f,
            softmax_loss_function=softmax_loss_function)
        self.outputs, self.losses, self.KL_objs, self.KL_costs = self.variational_decoder_with_buckets(
            targets=targets, buckets=buckets, decoder=decoder_f, latent_dec=latent_dec_f,
            sample=sample_f, softmax_loss_function=softmax_loss_function)

        # If we use output projection, we need to project outputs for decoding.
        if output_projection is not None:
            for b in range(len(buckets)):
                self.outputs[b] = [
                    tf.matmul(output, output_projection[0]) + output_projection[1]
                    for output in self.outputs[b]
                ]
        # Gradients and SGD update operation for training the model.
        params = tf.trainable_variables()
        if not forward_only:
            self.gradient_norms = []
            self.updates = []
            for b in range(len(buckets)):
                total_loss = self.losses[b] + self.KL_objs[b]
                gradients = tf.gradients(total_loss, params)
                clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                                 max_gradient_norm)
                self.gradient_norms.append(norm)
                self.updates.append(optimizer.apply_gradients(
                    zip(clipped_gradients, params), global_step=self.global_step))

        self.saver = tf.train.Saver(tf.global_variables(), max_to_keep=3)

    def sampled_loss(self, inputs, labels):
        dtype = tf.float32
        labels = tf.reshape(labels, [-1, 1])
        # We need to compute the sampled_softmax_loss using 32bit floats to
        # avoid numerical instabilities.
        self.local_w_t = tf.cast(self.w_t, tf.float32)
        self.local_b = tf.cast(self.b, tf.float32)
        local_inputs = tf.cast(inputs, tf.float32)
        local_labels = tf.cast(labels, tf.float32)
        return tf.cast(
            tf.nn.sampled_softmax_loss(weights=self.local_w_t, biases=self.local_b, inputs=local_inputs,
                                       labels=local_labels,
                                       num_sampled=512, num_classes=self.target_vocab_size),
            dtype)

    def variational_decoder_with_buckets(self,
                                         targets,
                                         buckets, decoder, latent_dec, sample,
                                         softmax_loss_function):
        means = self.means.copy()
        logvars = self.logvars.copy()
        decoder_inputs = self.decoder_inputs.copy()
        self._target = targets
        self._weights = self.target_weights.copy()
        self._softmax_loss_function = softmax_loss_function
        per_example_loss = False
        name = None

        """Create a sequence-to-sequence model with support for bucketing.
        """
        if len(self._target) < buckets[-1][1]:
            raise ValueError("Length of targets (%d) must be at least that of last"
                             "bucket (%d)." % (len(self._target), buckets[-1][1]))
        if len(self._weights) < buckets[-1][1]:
            raise ValueError("Length of weights (%d) must be at least that of last"
                             "bucket (%d)." % (len(self._weights), buckets[-1][1]))

        all_inputs = decoder_inputs + self._target + self._weights
        self._losses = []
        self._outputs = []
        self._KL_objs = []
        self._KL_costs = []
        with ops.name_scope(name, "variational_decoder_with_buckets", all_inputs):
            for j, bucket in enumerate(buckets):
                with variable_scope.variable_scope(variable_scope.get_variable_scope(),
                                                   reuse=True if j > 0 else None):

                    self._latent_vector, self._kl_obj, self._kl_cost = sample(means[j], logvars[j])
                    decoder_initial_state = latent_dec(self._latent_vector)

                    self._bucket_outputs, _ = decoder(decoder_initial_state, decoder_inputs[:bucket[1]])
                    self._outputs.append(self._bucket_outputs)
                    self.total_size = math_ops.add_n(self._weights[:bucket[1]])
                    self.total_size += 1e-9
                    self._KL_objs.append(tf.reduce_mean(self._kl_obj / self.total_size))
                    self._KL_costs.append(tf.reduce_mean(self._kl_cost / self.total_size))
                    if per_example_loss:
                        self._losses.append(seq2seq_helper.sequence_loss_by_example(
                            self._outputs[-1], self._target[:bucket[1]], self._weights[:bucket[1]],
                            softmax_loss_function=self._softmax_loss_function))
                    else:
                        self.our_loss = seq2seq_helper.sequence_loss(
                            self._outputs[-1], self._target[:bucket[1]], self._weights[:bucket[1]],
                            softmax_loss_function=self._softmax_loss_function)
                        self._losses.append(self.our_loss)

        return self._outputs.copy(), self._losses, self._KL_objs, self._KL_costs

    def step(self, session, encoder_inputs, decoder_inputs, target_weights,
             bucket_id, forward_only, prob, beam_size=1):
        """Run a step of the model feeding the given inputs.

        Args:
          session: tensorflow session to use.
          encoder_inputs: list of numpy int vectors to feed as encoder inputs.
          decoder_inputs: list of numpy int vectors to feed as decoder inputs.
          target_weights: list of numpy float vectors to feed as target weights.
          bucket_id: which bucket of the model to use.
          forward_only: whether to do the backward step or only forward.

        Returns:
          A triple consisting of gradient norm (or None if we did not do backward),
          average perplexity, and the outputs.

        Raises:
          ValueError: if length of encoder_inputs, decoder_inputs, or
            target_weights disagrees with bucket size for the specified bucket_id.
        """
        # Check if the sizes match.
        encoder_size, decoder_size = self.buckets[bucket_id]
        if len(encoder_inputs) != encoder_size:
            raise ValueError("Encoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(encoder_inputs), encoder_size))
        if len(decoder_inputs) != decoder_size:
            raise ValueError("Decoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(decoder_inputs), decoder_size))
        if len(target_weights) != decoder_size:
            raise ValueError("Weights length must be equal to the one in bucket,"
                             " %d != %d." % (len(target_weights), decoder_size))

        # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
        input_feed = {}
        for l in range(encoder_size):
            input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]
        for l in range(decoder_size):
            input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
            input_feed[self.target_weights[l].name] = target_weights[l]
        if self.word_dropout_keep_prob < 1:
            input_feed[self.replace_input.name] = np.full((self.batch_size), data_utils.UNK_ID, dtype=np.int32)

        # Since our targets are decoder inputs shifted by one, we need one more.
        last_target = self.decoder_inputs[decoder_size].name
        input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)
        if not prob:
            input_feed[self.logvars[bucket_id]] = np.full((self.batch_size, self.latent_dim), -800.0, dtype=np.float32)

        # Output feed: depends on whether we do a backward step or not.
        if not forward_only:
            output_feed = [self.updates[bucket_id],  # Update Op that does SGD.
                           self.gradient_norms[bucket_id],  # Gradient norm.
                           self.losses[bucket_id],
                           self.KL_costs[bucket_id]]  # Loss for this batch.
        else:
            output_feed = [self.losses[bucket_id], self.KL_costs[bucket_id]]  # Loss for this batch.
            for l in range(decoder_size):  # Output logits.
                output_feed.append(self.outputs[bucket_id][l])

        # len(session.run(self.encoder_inputs, input_feed))
        pdb.set_trace()
        tf.nn.sampled_softmax_loss(weights=self.local_w_t, biases=self.local_b, inputs=self._outputs[-1][0], labels=self._target[:19][0], num_sampled=512, num_classes=20000)
        seq2seq_helper.sequence_loss_by_example(self._outputs[-1], self._target[:19], self._weights[:19], softmax_loss_function=self._softmax_loss_function)
        seq2seq_helper.sequence_loss(self._outputs[-1], self._target[:19], self._weights[:19], softmax_loss_function=self._softmax_loss_function)
        session.run(self.our_loss, input_feed)
        session.run(seq2seq_helper.sequence_loss_by_example(self._outputs[-1], self._target[:19], self._weights[:19],
                                                            softmax_loss_function=self._softmax_loss_function),
                    input_feed)

        session.run(
            tf.contrib.seq2seq.sequence_loss(logits=tf.concat(self._outputs, 0), targets=tf.stack(self._target[:19], 0),
                                             weights=tf.stack(self._weights[:19], 0)), input_feed)
        session.run(seq2seq_helper.sequence_loss_by_example(self._outputs[-1], self._target[:19], self._weights[:19],
                                                            softmax_loss_function=self._softmax_loss_function),
                    input_feed)
        session.run(seq2seq_helper.sequence_loss(self._outputs[-1], self._target[:19], self._weights[:19],
                                                 softmax_loss_function=None), input_feed)
        session.run(tf.nn.softmax_cross_entropy_with_logits(logits=self._outputs[-1][0], labels=tf.transpose(
            tf.expand_dims(self._target[:19][0], 0))), input_feed)
        session.run(tf.contrib.seq2seq.sequence_loss(logits=self._outputs[-1][0], targets=self._target[:19][0],
                                                     weights=self._weights[:19][0],
                                                     softmax_loss_function=self._softmax_loss_function), input_feed)
        # session.run(seq2seq_helper.sequence_loss(self._outputs[-1], self._target[:19], self._weights[:19], softmax_loss_function=self._softmax_loss_function), input_feed)
        # out, tar, wei = session.run([self._outputs[-1], self._target[:19], self._weights[:19]], input_feed)
        # seq2seq_helper.sequence_loss(out, tar, wei, softmax_loss_function=self._softmax_loss_function)

        outputs = session.run(output_feed, input_feed)
        if not forward_only:
            return outputs[1], outputs[2], outputs[3], None  # Gradient norm, loss, KL divergence, no outputs.
        else:
            return None, outputs[0], outputs[1], outputs[2:]  # no gradient norm, loss, KL divergence, outputs.

    def encode_to_latent(self, session, encoder_inputs, bucket_id):

        # Check if the sizes match.
        encoder_size, _ = self.buckets[bucket_id]
        if len(encoder_inputs) != encoder_size:
            raise ValueError("Encoder length must be equal to the one in bucket,"
                             " %d != %d." % (len(encoder_inputs), encoder_size))

        input_feed = {}
        for l in range(encoder_size):
            input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]

        output_feed = [self.means[bucket_id], self.logvars[bucket_id]]
        means, logvars = session.run(output_feed, input_feed)

        return means, logvars

    def decode_from_latent(self, session, means, logvars, bucket_id, decoder_inputs, target_weights):

        _, decoder_size = self.buckets[bucket_id]
        # Input feed: means.
        input_feed = {self.means[bucket_id]: means}
        input_feed[self.logvars[bucket_id]] = logvars

        for l in range(decoder_size):
            input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
            input_feed[self.target_weights[l].name] = target_weights[l]
        if self.word_dropout_keep_prob < 1:
            input_feed[self.replace_input.name] = np.full((self.batch_size), data_utils.UNK_ID, dtype=np.int32)

        last_target = self.decoder_inputs[decoder_size].name
        input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)
        output_feed = []
        for l in range(decoder_size):  # Output logits.
            output_feed.append(self.outputs[bucket_id][l])

        outputs = session.run(output_feed, input_feed)

        return outputs

    def get_batch(self, data, bucket_id):
        """Get a random batch of data from the specified bucket, prepare for step.

        To feed data in step(..) it must be a list of batch-major vectors, while
        data here contains single length-major cases. So the main logic of this
        function is to re-index data cases to be in the proper format for feeding.

        Args:
          data: a tuple of size len(self.buckets) in which each element contains
            lists of pairs of input and output data that we use to create a batch.
          bucket_id: integer, which bucket to get the batch for.

        Returns:
          The triple (encoder_inputs, decoder_inputs, target_weights) for
          the constructed batch that has the proper format to call step(...) later.
        """
        encoder_size, decoder_size = self.buckets[bucket_id]
        encoder_inputs, decoder_inputs = [], []

        # Get a random batch of encoder and decoder inputs from data,
        # pad them if needed, reverse encoder inputs and add GO to decoder.
        for _ in range(self.batch_size):
            encoder_input, decoder_input = random.choice(data[bucket_id])

            # Encoder inputs are padded and then reversed.
            encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

            # Decoder inputs get an extra "GO" symbol, and are padded then.
            decoder_pad_size = decoder_size - len(decoder_input) - 1
            decoder_inputs.append([data_utils.GO_ID] + decoder_input +
                                  [data_utils.PAD_ID] * decoder_pad_size)

        # Now we create batch-major vectors from the data selected above.
        batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in range(encoder_size):
            batch_encoder_inputs.append(
                np.array([encoder_inputs[batch_idx][length_idx]
                          for batch_idx in range(self.batch_size)], dtype=np.int32))

        # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
        for length_idx in range(decoder_size):
            batch_decoder_inputs.append(
                np.array([decoder_inputs[batch_idx][length_idx]
                          for batch_idx in range(self.batch_size)], dtype=np.int32))

            # Create target_weights to be 0 for targets that are padding.
            batch_weight = np.ones(self.batch_size, dtype=np.float32)
            for batch_idx in range(self.batch_size):
                # We set weight to 0 if the corresponding target is a PAD symbol.
                # The corresponding target is decoder_input shifted by 1 forward.
                if length_idx < decoder_size - 1:
                    target = decoder_inputs[batch_idx][length_idx + 1]
                if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
                    batch_weight[batch_idx] = 0.0
            batch_weights.append(batch_weight)
        return batch_encoder_inputs, batch_decoder_inputs, batch_weights
