# Copyright 2017 Abien Fred Agarap. All Rights Reserved.
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

"""Implementation of the GRU+SVM model for Intrusion Detection"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

__version__ = '0.3.0'
__author__ = 'Abien Fred Agarap'

import argparse
import data
import numpy as np
import os
import sys
import tensorflow as tf
import time

# hyper-parameters
BATCH_SIZE = 256
CELL_SIZE = 256
DROPOUT_P_KEEP = 0.55
HM_EPOCHS = 1
N_CLASSES = 2
SEQUENCE_LENGTH = 21
SVM_C = 1

# learning rate decay parameters
INITIAL_LEARNING_RATE = 0.01
LEARNING_RATE_DECAY_FACTOR = 0.995
NUM_EPOCHS_PER_DECAY = 1


class GruSvm:

    def __init__(self, train_data, validation_data, checkpoint_path, log_path, model_name):
        self.train_data = train_data
        self.validation_data = validation_data
        self.checkpoint_path = checkpoint_path
        self.log_path = log_path
        self.model_name = model_name

        def __graph__():
            """Build the inference graph"""
            with tf.name_scope('input'):
                # [BATCH_SIZE, SEQUENCE_LENGTH, 10]
                x_input = tf.placeholder(dtype=tf.float32, shape=[None, SEQUENCE_LENGTH, 10], name='x_input')

                # [BATCH_SIZE, N_CLASSES]
                y_input = tf.placeholder(dtype=tf.float32, shape=[None, N_CLASSES], name='y_input')

            state = tf.placeholder(dtype=tf.float32, shape=[None, CELL_SIZE], name='initial_state')

            p_keep = tf.placeholder(dtype=tf.float32, name='p_keep')

            # [None]
            evaluation_value = tf.placeholder(dtype=tf.float32, name='evaluation_value')

            cell = tf.contrib.rnn.GRUCell(CELL_SIZE)
            drop_cell = tf.contrib.rnn.DropoutWrapper(cell, input_keep_prob=p_keep)

            # outputs: [BATCH_SIZE, SEQUENCE_LENGTH, CELL_SIZE]
            # states: [BATCH_SIZE, CELL_SIZE]
            outputs, states = tf.nn.dynamic_rnn(drop_cell, x_input, initial_state=state, dtype=tf.float32)

            states = tf.identity(states, name='H')

            with tf.name_scope('final_training_ops'):
                with tf.name_scope('weights'):
                    xav_init = tf.contrib.layers.xavier_initializer
                    weight = tf.get_variable('weights', shape=[CELL_SIZE, N_CLASSES], initializer=xav_init())
                    self.variable_summaries(weight)
                with tf.name_scope('biases'):
                    bias = tf.get_variable('biases', initializer=tf.constant(0.1, shape=[N_CLASSES]))
                    self.variable_summaries(bias)
                hf = tf.transpose(outputs, [1, 0, 2])
                last = tf.gather(hf, int(hf.get_shape()[0]) - 1)
                with tf.name_scope('Wx_plus_b'):
                    output = tf.matmul(last, weight) + bias
                    tf.summary.histogram('pre-activations', output)

            g_step = tf.placeholder(dtype=tf.float32, shape=[], name='global_step')
            learning_rate = tf.train.exponential_decay(learning_rate=INITIAL_LEARNING_RATE,
                                                       global_step=g_step, decay_steps=NUM_EPOCHS_PER_DECAY,
                                                       decay_rate=LEARNING_RATE_DECAY_FACTOR, staircase=True,
                                                       name='learning_rate')
            tf.summary.scalar('learning_rate', learning_rate)

            # L2-SVM
            with tf.name_scope('svm'):
                regularization_loss = 0.5 * tf.reduce_sum(tf.square(weight))
                hinge_loss = tf.reduce_sum(
                    tf.square(tf.maximum(tf.zeros([BATCH_SIZE, N_CLASSES]), 1 - y_input * output)))
                with tf.name_scope('loss'):
                    loss = regularization_loss + SVM_C * hinge_loss
            tf.summary.scalar('loss', loss)

            optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate).minimize(loss)

            with tf.name_scope('accuracy'):
                predicted_class = tf.sign(output)
                predicted_class = tf.identity(predicted_class, name='prediction')
                with tf.name_scope('correct_prediction'):
                    correct = tf.equal(tf.argmax(predicted_class, 1), tf.argmax(y_input, 1))
                with tf.name_scope('accuracy'):
                    accuracy = tf.reduce_mean(tf.cast(correct, 'float'))
            tf.summary.scalar('accuracy', accuracy)

            # merge all the summaries collected from the TF graph
            merged = tf.summary.merge_all()

            with tf.name_scope('evaluation'):
                evaluation_predicted_class = tf.sign(output)
                evaluation_predicted_class = tf.identity(evaluation_predicted_class, name='prediction')
                with tf.name_scope('correct_prediction'):
                    evaluation_correct = tf.equal(tf.argmax(evaluation_predicted_class, 1), tf.argmax(y_input, 1))
                with tf.name_scope('accuracy'):
                    evaluation_accuracy = tf.reduce_mean(tf.cast(evaluation_correct, 'float'))

            evaluation_summary_op = tf.summary.merge([tf.summary.scalar('evaluation_accuracy', evaluation_value),
                                                      tf.summary.scalar('evaluation_loss', loss)], name='evaluation')

            # set class properties
            self.x_input = x_input
            self.y_input = y_input
            self.p_keep = p_keep
            self.loss = loss
            self.optimizer = optimizer
            self.state = state
            self.states = states
            self.g_step = g_step
            self.learning_rate = learning_rate
            self.accuracy = accuracy
            self.merged = merged
            self.evaluation_value = evaluation_value
            self.evaluation_accuracy = evaluation_accuracy
            self.evaluation_summary_op = evaluation_summary_op

        sys.stdout.write('\n<log> Building Graph...')
        __graph__()
        sys.stdout.write('</log>\n')

    def train(self):
        """Train the model"""

        if not os.path.exists(path=self.checkpoint_path):
            os.mkdir(path=self.checkpoint_path)

        saver = tf.train.Saver(max_to_keep=1000)

        # initialize H (current_state) with values of zeros
        current_state = np.zeros([BATCH_SIZE, CELL_SIZE])

        # variables initializer
        init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

        # get the time tuple
        timestamp = str(time.asctime())

        train_writer = tf.summary.FileWriter(logdir=self.log_path + timestamp, graph=tf.get_default_graph())

        with tf.Session() as sess:
            sess.run(init_op)

            checkpoint = tf.train.get_checkpoint_state(self.checkpoint_path)

            if checkpoint and checkpoint.model_checkpoint_path:
                saver = tf.train.import_meta_graph(checkpoint.model_checkpoint_path + '.meta')
                saver.restore(sess, tf.train.latest_checkpoint(self.checkpoint_path))

            coord = tf.train.Coordinator()
            threads = tf.train.start_queue_runners(coord=coord)

            try:
                step = 0
                while not coord.should_stop():
                    train_example_batch, train_label_batch = sess.run([self.train_data[0], self.train_data[1]])

                    # dictionary for key-value pair input for training
                    feed_dict = {self.x_input: train_example_batch, self.y_input: train_label_batch,
                                 self.state: current_state,
                                 self.g_step: step, self.p_keep: DROPOUT_P_KEEP}

                    train_summary, lr, _, next_state = sess.run(
                        [self.merged, self.learning_rate, self.optimizer, self.states],
                        feed_dict=feed_dict)

                    # Display training loss and accuracy every 100 steps and at step 0
                    if step % 100 == 0:
                        # get train loss and accuracy
                        train_loss, train_accuracy = sess.run([self.loss, self.accuracy], feed_dict=feed_dict)

                        # display train loss and accuracy
                        print('step [{}] train -- loss : {}, accuracy : {}'.format(step, train_loss, train_accuracy))

                        # write the train summary
                        train_writer.add_summary(train_summary, step)

                        # save the model at current step
                        saver.save(sess, self.checkpoint_path + self.model_name, global_step=step)

                    # Display validation loss and accuracy every 100 steps
                    if step % 100 == 0 and step > 0:
                        # retrieve validation data
                        test_example_batch, test_label_batch = sess.run([self.validation_data[0],
                                                                         self.validation_data[1]])
                        # dictionary for key-value pair input for validation
                        feed_dict = {self.x_input: test_example_batch, self.y_input: test_label_batch,
                                     self.state: np.zeros([BATCH_SIZE, CELL_SIZE]), self.p_keep: 1.0}

                        # get validation loss and accuracy
                        evaluation_loss, evaluation_accuracy = sess.run([self.loss, self.evaluation_accuracy],
                                                                        feed_dict=feed_dict)

                        # dictionary for key-value pair input for summary writing
                        feed_dict = {self.evaluation_value: evaluation_accuracy, self.loss: evaluation_loss}

                        # get summary of validation
                        evaluation_summary = sess.run(self.evaluation_summary_op, feed_dict=feed_dict)

                        # display validation loss and accuracy
                        print('step [{}] validation -- loss : {}, accuracy : {}'.format(step, evaluation_loss,
                                                                                        evaluation_accuracy))

                        # write the validation summary
                        train_writer.add_summary(evaluation_summary, step)

                    current_state = next_state
                    step += 1
            except tf.errors.OutOfRangeError:
                print('EOF -- training done at step {}'.format(step))
            except KeyboardInterrupt:
                print('Training interrupted at {}'.format(step))
            finally:
                train_writer.close()
                coord.request_stop()
            coord.join(threads)

            saver.save(sess, self.checkpoint_path + self.model_name, global_step=step)

    @staticmethod
    def variable_summaries(var):
        with tf.name_scope('summaries'):
            mean = tf.reduce_mean(var)
            tf.summary.scalar('mean', mean)
            with tf.name_scope('stddev'):
                stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
            tf.summary.scalar('stddev', stddev)
            tf.summary.scalar('max', tf.reduce_max(var))
            tf.summary.scalar('min', tf.reduce_min(var))
            tf.summary.histogram('histogram', var)


def parse_args():
    parser = argparse.ArgumentParser(description='GRU+SVM for Intrusion Detection')
    group = parser.add_argument_group('Arguments')
    group.add_argument('-t', '--train_dataset', required=True, type=str,
                       help='path of the training dataset to be used')
    group.add_argument('-v', '--validation_dataset', required=True, type=str,
                       help='path of the validation dataset to be used')
    group.add_argument('-c', '--checkpoint_path', required=True, type=str,
                       help='path where to save the trained model')
    group.add_argument('-l', '--log_path', required=True, type=str,
                       help='path where to save the TensorBoard logs')
    group.add_argument('-m', '--model_name', required=True, type=str,
                       help='filename for the trained model')
    arguments = parser.parse_args()
    return arguments


def main(argv):

    # get the train data
    # features: train_data[0], labels: train_data[1]
    train_data = data.input_pipeline(path=argv.train_dataset, batch_size=BATCH_SIZE,
                                     num_classes=N_CLASSES, num_epochs=HM_EPOCHS)

    # get the validation data
    # features: validation_data[0], labels: validation_data[1]
    validation_data = data.input_pipeline(path=argv.validation_dataset, batch_size=BATCH_SIZE,
                                          num_classes=N_CLASSES, num_epochs=1)

    # instantiate the model
    model = GruSvm(train_data=train_data, validation_data=validation_data, checkpoint_path=argv.checkpoint_path,
                   log_path=argv.log_path, model_name=argv.model_name)

    # train the model
    model.train()

if __name__ == '__main__':
    args = parse_args()

    main(argv=args)
