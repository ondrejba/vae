import os
from enum import Enum
import numpy as np
import tensorflow as tf
from . import utils
from .model import Model


class SG_VAE(Model):
    # https://github.com/ericjang/gumbel-softmax/blob/master/gumbel_softmax_vae_v2.ipynb

    MODEL_NAMESPACE = "model"
    TRAINING_NAMESPACE = "training"

    class LossType(Enum):
        SIGMOID_CROSS_ENTROPY = 1
        L2 = 2

    class KLType(Enum):
        CATEGORICAL = 1
        RELAXED = 2

    def __init__(self, input_shape, encoder_filters, encoder_filter_sizes, encoder_strides, encoder_neurons,
                 decoder_neurons, decoder_filters, decoder_filter_sizes, decoder_strides, num_distributions,
                 num_classes, loss_type, weight_decay, learning_rate, kl_type, disable_kl_loss=False,
                 straight_through=False, fix_cudnn=False):

        super(SG_VAE, self).__init__(fix_cudnn=fix_cudnn)

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
        self.num_distributions = num_distributions
        self.num_classes = num_classes
        self.loss_type = loss_type
        self.weight_decay = weight_decay
        self.learning_rate = learning_rate
        self.disable_kl_loss = disable_kl_loss
        self.kl_type = kl_type

        self.input_pl = None
        self.input_flat_t = None
        self.mu_t = None
        self.log_var_t = None
        self.sd_t = None
        self.var_t = None
        self.mean_sq_t = None
        self.kl_divergence_t = None
        self.kl_loss_t = None
        self.reg_loss_t = None
        self.noise_t = None
        self.sd_noise_t = None
        self.sample_t = None
        self.logits_t = None
        self.flat_logits_t = None
        self.output_t = None
        self.output_loss_t = None
        self.loss_t = None
        self.step_op = None
        self.session = None
        self.saver = None
        self.straight_through = straight_through

    def encode(self, inputs, batch_size):

        num_steps = int(np.ceil(inputs.shape[0] / batch_size))
        encodings = []

        for step_idx in range(num_steps):

            batch_slice = np.index_exp[step_idx * batch_size:(step_idx + 1) * batch_size]

            tmp_encoding = self.session.run(self.sample_t, feed_dict={
                self.input_pl: inputs[batch_slice],
                self.is_training_pl: False,
                self.temperature_pl: 1.0        # the value doesn't matter
            })

            encodings.append(tmp_encoding)

        encodings = np.concatenate(encodings, axis=0)

        return encodings

    def predict(self, num_samples):

        outputs = self.session.run(self.output_t, feed_dict={
            self.probs_t: np.ones(
                (num_samples, self.num_distributions, self.num_classes), dtype=np.float32
            ) / self.num_classes,
            self.is_training_pl: False,
            self.temperature_pl: 1.0        # the value doesn't matter
        })

        return outputs[:, :, :, 0]

    def train(self, samples, temperature):

        _, loss, output_loss, kl_loss, reg_loss = self.session.run(
            [self.step_op, self.loss_t, self.output_loss_t, self.kl_loss_t, self.reg_loss_t], feed_dict={
                self.input_pl: samples,
                self.is_training_pl: True,
                self.temperature_pl: temperature
            }
        )

        return loss, output_loss, kl_loss, reg_loss

    def get_log_likelihood(self, data, batch_size=100):

        num_steps = int(np.ceil(data.shape[0] / batch_size))
        lls = []

        for idx in range(num_steps):

            ll = self.session.run(self.full_output_loss_t, feed_dict={
                self.input_pl: data[idx * batch_size: (idx + 1) * batch_size],
                self.is_training_pl: False,
                self.temperature_pl: 1.0
            })

            lls.append(ll)

        lls = - np.concatenate(lls, axis=0)

        return lls

    def build_all(self):

        self.build_placeholders()
        self.build_network()
        self.build_training()

        self.saver = tf.train.Saver(
            var_list=tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.MODEL_NAMESPACE)
        )

    def build_placeholders(self):

        self.input_pl = tf.placeholder(tf.float32, shape=(None, *self.input_shape), name="input_pl")
        self.input_flat_t = tf.reshape(self.input_pl, shape=(tf.shape(self.input_pl)[0], self.flat_input_shape))
        self.temperature_pl = tf.placeholder(tf.float32, shape=[], name="temperature_pl")
        self.is_training_pl = tf.placeholder(tf.bool, shape=[], name="is_training_pl")

    def build_network(self):

        with tf.variable_scope(self.MODEL_NAMESPACE):

            # encoder
            x = self.build_encoder(self.input_pl)

            # middle
            self.sample_t, self.kl_divergence_t, self.probs_t = self.build_middle(x)

            # decoder
            self.logits_t, self.flat_logits_t, self.output_t = self.build_decoder(self.sample_t)

    def build_encoder(self, input_t, share_weights=False):

        x = tf.expand_dims(input_t, axis=-1)

        with tf.variable_scope("encoder", reuse=share_weights):

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

        return x

    def build_middle(self, input_t, share_weights=False):

        with tf.variable_scope("middle", reuse=share_weights):

            logits_t = tf.layers.dense(
                input_t, self.num_distributions * self.num_classes, activation=None,
                kernel_regularizer=utils.get_weight_regularizer(self.weight_decay)
            )
            logits_reshaped_t = tf.reshape(
                logits_t, shape=(tf.shape(logits_t)[0], self.num_distributions, self.num_classes)
            )

            sg_dist = tf.contrib.distributions.RelaxedOneHotCategorical(self.temperature_pl, logits=logits_reshaped_t)
            cat_dist = tf.contrib.distributions.OneHotCategorical(logits=logits_reshaped_t)

            sg_sample_t = sg_dist.sample()

            if self.straight_through:
                sg_sample_hard_t = tf.cast(tf.one_hot(tf.argmax(sg_sample_t, -1), self.num_classes), tf.float32)
                sg_sample_t = tf.stop_gradient(sg_sample_hard_t - sg_sample_t) + sg_sample_t

            cat_sample_t = tf.cast(cat_dist.sample(), tf.float32)

            sg_sample_reshaped_t = tf.reshape(
                sg_sample_t, shape=(tf.shape(sg_sample_t)[0], self.num_distributions * self.num_classes)
            )
            cat_sample_reshaped_t = tf.reshape(
                cat_sample_t, shape=(tf.shape(cat_sample_t)[0], self.num_distributions * self.num_classes)
            )

            prior_logits_t = tf.ones_like(logits_reshaped_t) / self.num_classes

            if self.kl_type == self.KLType.CATEGORICAL or self.straight_through:
                prior_cat_dist = tf.contrib.distributions.OneHotCategorical(logits=prior_logits_t)
                kl_divergence_t = tf.contrib.distributions.kl_divergence(cat_dist, prior_cat_dist)
            else:
                prior_sg_dist = tf.contrib.distributions.RelaxedOneHotCategorical(
                    self.temperature_pl, logits=prior_logits_t
                )
                kl_divergence_t = sg_dist.log_prob(sg_sample_t) - prior_sg_dist.log_prob(sg_sample_t)

            sample_reshaped_t = tf.where(self.is_training_pl, x=sg_sample_reshaped_t, y=cat_sample_reshaped_t)

        return sample_reshaped_t, kl_divergence_t, logits_reshaped_t

    def build_decoder(self, input_t, share_weights=False):

        with tf.variable_scope("decoder", reuse=share_weights):

            x = input_t

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

        logits_t = x
        flat_logits_t = tf.layers.flatten(x)

        if self.loss_type == self.LossType.SIGMOID_CROSS_ENTROPY:
            output_t = tf.nn.sigmoid(logits_t)
        else:
            output_t = logits_t

        return logits_t, flat_logits_t, output_t

    def build_training(self):

        with tf.variable_scope(self.TRAINING_NAMESPACE):

            if self.loss_type == self.LossType.SIGMOID_CROSS_ENTROPY:
                self.full_output_loss_t = tf.reduce_sum(
                    tf.losses.sigmoid_cross_entropy(
                        multi_class_labels=self.input_flat_t, logits=self.flat_logits_t,
                        reduction=tf.losses.Reduction.NONE
                    ),
                    axis=1
                )
            else:
                self.full_output_loss_t = tf.reduce_sum(
                    tf.losses.mean_squared_error(
                        labels=self.input_flat_t, predictions=self.flat_logits_t,
                        reduction=tf.losses.Reduction.NONE
                    ),
                    axis=1
                )

            self.output_loss_t = tf.reduce_mean(self.full_output_loss_t, axis=0)

            self.kl_loss_t = tf.reduce_mean(tf.reduce_sum(self.kl_divergence_t, axis=1))

            self.reg_loss_t = tf.add_n(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))

            if self.disable_kl_loss:
                self.loss_t = self.output_loss_t + self.reg_loss_t
            else:
                self.loss_t = self.output_loss_t + self.kl_loss_t + self.reg_loss_t

            self.step_op = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(self.loss_t)
