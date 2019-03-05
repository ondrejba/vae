# https://github.com/nadavbh12/VQ-VAE
# https://github.com/deepmind/sonnet/blob/master/sonnet/python/modules/nets/vqvae.py
# https://arxiv.org/pdf/1711.00937.pdf
# http://bayesiandeeplearning.org/2017/papers/54.pdf


import os
from enum import Enum
import numpy as np
import tensorflow as tf
from . import utils


class VQ_VAE:

    MODEL_NAMESPACE = "model"
    TRAINING_NAMESPACE = "training"

    class LossType(Enum):

        SIGMOID_CROSS_ENTROPY = 1
        L2 = 2

    def __init__(self, input_shape, encoder_filters, encoder_filter_sizes, encoder_strides, encoder_neurons,
                 decoder_neurons, decoder_filters, decoder_filter_sizes, decoder_strides, latent_space_size,
                 num_embeddings, embedding_size, loss_type, weight_decay, learning_rate):

        assert loss_type in self.LossType
        assert len(encoder_filters) == len(encoder_filter_sizes) == len(encoder_strides)
        assert len(decoder_filters) == len(decoder_filter_sizes) == len(decoder_strides)

        self.input_shape = input_shape
        self.flat_input_shape = int(np.prod(self.input_shape))
        self.encoder_filters = encoder_filters
        self.encoder_filter_sizes = encoder_filter_sizes
        self.encoder_strides = encoder_strides
        self.encoder_neurons = encoder_neurons
        self.decoder_neurons = decoder_neurons
        self.decoder_filters = decoder_filters
        self.decoder_filter_sizes = decoder_filter_sizes
        self.decoder_strides = decoder_strides
        self.latent_space_size = latent_space_size
        self.num_embeddings = num_embeddings
        self.embedding_size = embedding_size
        self.loss_type = loss_type
        self.weight_decay = weight_decay
        self.learning_rate = learning_rate

        self.input_pl = None
        self.input_flat_t = None
        self.sample_t = None
        self.logits_t = None
        self.flat_logits_t = None
        self.output_t = None
        self.reg_loss_t = None
        self.output_loss_t = None
        self.loss_t = None
        self.step_op = None
        self.session = None

        self.build_placeholders()
        self.build_network()
        self.build_training()

        self.saver = tf.train.Saver(
            var_list=tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.MODEL_NAMESPACE)
        )

    def predict(self, num_samples):

        outputs = self.session.run(self.output_t, feed_dict={
            self.sample_t: np.random.normal(loc=0, scale=1, size=(num_samples, self.latent_space_size))

        })

        return outputs[:, :, :, 0]

    def train(self, samples):

        _, loss, output_loss, reg_loss = self.session.run(
            [self.step_op, self.loss_t, self.output_loss_t, self.reg_loss_t], feed_dict={
                self.input_pl: samples
            }
        )

        return loss, output_loss, reg_loss

    def build_placeholders(self):

        self.input_pl = tf.placeholder(tf.float32, shape=(None, *self.input_shape), name="input_pl")

    def build_network(self):

        self.embeddings = tf.get_variable(
            "embeddings", shape=(self.num_embeddings, self.embedding_size), dtype=tf.float32
        )



        with tf.variable_scope(self.MODEL_NAMESPACE):

            self.input_flat_t = tf.reshape(self.input_pl, shape=(tf.shape(self.input_pl)[0], self.flat_input_shape))

            # encoder
            x = tf.expand_dims(self.input_pl, axis=-1)

            with tf.variable_scope("encoder"):

                for idx in range(len(self.encoder_filters)):
                    with tf.variable_scope("conv{:d}".format(idx + 1)):
                        x = tf.layers.conv2d(
                            x, self.encoder_filters[idx], self.encoder_filter_sizes[idx], self.encoder_strides[idx],
                            padding="SAME", activation=tf.nn.relu,
                            kernel_regularizer=utils.get_weight_regularizer(self.weight_decay)
                        )

                x = tf.layers.flatten(x)

                for idx, neurons in enumerate(self.encoder_neurons):
                    with tf.variable_scope("fc{:d}".format(idx + 1)):
                        x = tf.layers.dense(
                            x, neurons, activation=tf.nn.relu,
                            kernel_regularizer=utils.get_weight_regularizer(self.weight_decay)
                        )

            # middle
            with tf.variable_scope("middle"):

                self.sample_t = tf.layers.dense(
                    x, self.latent_space_size, activation=None,
                    kernel_regularizer=utils.get_weight_regularizer(self.weight_decay)
                )

            # decoder
            with tf.variable_scope("decoder"):

                x = self.sample_t

                for idx, neurons in enumerate(self.decoder_neurons):
                    with tf.variable_scope("fc{:d}".format(idx + 1)):
                        x = tf.layers.dense(
                            x, neurons, activation=tf.nn.relu,
                            kernel_regularizer=utils.get_weight_regularizer(self.weight_decay)
                        )

                x = x[:, tf.newaxis, tf.newaxis, :]

                for idx in range(len(self.decoder_filters)):
                    with tf.variable_scope("conv{:d}".format(idx + 1)):
                        x = tf.layers.conv2d_transpose(
                            x, self.decoder_filters[idx], self.decoder_filter_sizes[idx], self.decoder_strides[idx],
                            padding="VALID", activation=tf.nn.relu if idx != len(self.decoder_filters) - 1 else None,
                            kernel_regularizer=utils.get_weight_regularizer(self.weight_decay)
                        )

            self.logits_t = x
            self.flat_logits_t = tf.layers.flatten(x)
            self.output_t = tf.nn.sigmoid(self.logits_t)

    def build_training(self):

        with tf.variable_scope(self.TRAINING_NAMESPACE):

            if self.loss_type == self.LossType.SIGMOID_CROSS_ENTROPY:
                self.output_loss_t = tf.reduce_mean(
                    tf.reduce_sum(
                        tf.losses.sigmoid_cross_entropy(
                            multi_class_labels=self.input_flat_t, logits=self.flat_logits_t,
                            reduction=tf.losses.Reduction.NONE
                        ),
                        axis=1
                    ),
                    axis=0
                )
            else:
                self.output_loss_t = tf.reduce_mean(
                    tf.reduce_sum(
                        tf.losses.mean_squared_error(
                            labels=self.input_flat_t, logits=self.flat_logits_t,
                            predictions=tf.losses.Reduction.NONE
                        ),
                        axis=1
                    ),
                    axis=0
                )

            self.reg_loss_t = tf.add_n(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))

            self.loss_t = self.output_loss_t + self.reg_loss_t

            self.step_op = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.loss_t)

    def start_session(self):

        self.session = tf.Session()
        self.session.run(tf.global_variables_initializer())

    def stop_session(self):

        if self.session is not None:
            self.session.close()

    def save(self, path):

        dir_name = os.path.dirname(path)
        if not os.path.isdir(dir_name):
            os.makedirs(dir_name)

        self.saver.save(self.session, path)

    def load(self, path):

        self.saver.restore(self.session, path)