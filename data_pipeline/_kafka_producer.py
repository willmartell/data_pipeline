# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import time
import simplejson as json
from collections import defaultdict
from collections import namedtuple

from cached_property import cached_property
from kafka import create_message
from kafka.common import ProduceRequest
from yelp_crypto import encrypt_blob
from data_pipeline._position_data_tracker import PositionDataTracker
from data_pipeline.config import get_config
from data_pipeline.envelope import Envelope


_EnvelopeAndMessage = namedtuple("_EnvelopeAndMessage", ["envelope", "message"])
logger = get_config().logger


# prepare needs to be in the module top level so it can be serialized for
# multiprocessing
def _prepare(envelope_and_message):
    try:
        kwargs = {}
        if envelope_and_message.message.keys:
            kwargs['key'] = envelope_and_message.envelope.pack_keys(
                envelope_and_message.message.keys
            )

        return create_message(
            envelope_and_message.envelope.pack(envelope_and_message.message),
            **kwargs
        )
    except:
        logger.exception('Prepare failed')
        raise


class KafkaProducer(object):
    """The KafkaProducer deals with buffering messages that need to be published
    into Kafka, preparing them for publication, and ultimately publishing them.

    Args:
      producer_position_callback (function): The producer position callback is
        called when the KafkaProducer is instantiated, and every time messages
        are published to notify the producer of current position information of
        successfully published messages.
    """
    @cached_property
    def envelope(self):
        return Envelope()

    def __init__(self, producer_position_callback, dry_run=False):
        self.producer_position_callback = producer_position_callback
        self.dry_run = dry_run
        self.kafka_client = get_config().kafka_client
        self.position_data_tracker = PositionDataTracker()
        self._reset_message_buffer()
        self.skip_messages_with_pii = get_config().skip_messages_with_pii
        self.user = get_config().user
        self.acceptable_users = ['batch']

    def wake(self):
        """Should be called periodically if we're not otherwise waking up by
        publishing, to ensure that messages are actually published.
        """
        # if we haven't woken up in a while, we may need to flush messages
        self._flush_if_necessary()

    def publish(self, message):
        if  message.contains_pii:
            if self.skip_messages_with_pii or !(self.user in self.acceptable_users):
                return
            elif self._encrypt_message_with_pii(message) !=  True:
                return
        self._add_message_to_buffer(message)
        self.position_data_tracker.record_message_buffered(message)
        self._flush_if_necessary()
    
    def _encrypt_message_with_pii(message):
        """Encrypt message with key on machine, using AES.
        This method will only be called if the user has
        prod access. Returns None if the key was not found,
        or if the message could not be encrypted, and
        otherwise returns 1 and mutates the message
        to have an encrypted payload"""
        if key = self._retrieve_key():
            return self._encrypt_message_using_yelp_crypto(key, message)
        return None

    def _retrieve_key(self):
        try:
            #TODO(krane): fill in key-getting logic.

        except(Exception e):
            self.logger.log(
                    "Retrieving encryption key failed with traceback {}".format(e.traceback()
            )
            return False

    def _encrypt_message_using_yelp_crypto(self, key, message):
        payload = message.payload() or message.payload_data()
        if payload isinstance(dict):
            new_payload = encrypt_blob(key, json.dumps(payload))
            message.payload_data(json.loads(new_payload))
        else: 
            new_payload = encrypt_blob(key, payload)
            message.payload(new_payload)
        return True
            

    def flush_buffered_messages(self):
        produce_method = self._publish_produce_requests_dry_run if self.dry_run else self._publish_produce_requests
        produce_method(self._generate_produce_requests())
        self._reset_message_buffer()

    def close(self):
        self.flush_buffered_messages()
        self.kafka_client.close()

    def _publish_produce_requests(self, requests):
        # TODO(DATAPIPE-149|justinc): This should be a loop, where on each
        # iteration all produce requests for topics that succeeded are removed,
        # and all produce requests that failed are retried.  If all haven't
        # succeeded after a few tries, this should blow up.
        try:
            published_messages_count = 0
            responses = self.kafka_client.send_produce_request(
                payloads=requests,
                acks=1  # Written to disk on master
            )
            for response in responses:
                # TODO(DATAPIPE-149|justinc): This won't work if the error code
                # is non-zero
                self.position_data_tracker.record_messages_published(
                    response.topic,
                    response.offset,
                    len(self.message_buffer[response.topic])
                )
                published_messages_count += len(self.message_buffer[response.topic])
            # Don't let this return if we didn't publish all the messages
            assert published_messages_count == self.message_buffer_size
        except:
            logger.exception("Produce failed... fix in DATAPIPE-149")
            raise

    def _publish_produce_requests_dry_run(self, requests):
        for request in requests:
            topic = request.topic
            message_count = len(request.messages)
            self.position_data_tracker.record_messages_published(
                topic,
                -1,
                message_count
            )
            logger.debug("dry_run mode: Would have published {0} messages to {1}".format(
                message_count,
                topic
            ))

    def _is_ready_to_flush(self):
        time_limit = get_config().kafka_producer_flush_time_limit_seconds
        return (
            (time.time() - self.start_time) >= time_limit or
            self.message_buffer_size >= get_config().kafka_producer_buffer_size
        )

    def _flush_if_necessary(self):
        if self._is_ready_to_flush():
            self.flush_buffered_messages()

    def _add_message_to_buffer(self, message):
        topic = message.topic
        message = self._prepare_message(message)

        self.message_buffer[topic].append(message)
        self.message_buffer_size += 1

    def _generate_produce_requests(self):
        return [
            ProduceRequest(topic=topic, partition=0, messages=messages)
            for topic, messages in self._generate_prepared_topic_and_messages()
        ]

    def _generate_prepared_topic_and_messages(self):
        return self.message_buffer.iteritems()

    def _prepare_message(self, message):
        return _prepare(_EnvelopeAndMessage(envelope=self.envelope, message=message))

    def _reset_message_buffer(self):
        self.producer_position_callback(self.position_data_tracker.get_position_data())

        self.start_time = time.time()
        self.message_buffer = defaultdict(list)
        self.message_buffer_size = 0


class LoggingKafkaProducer(KafkaProducer):
    def _publish_produce_requests(self, requests):
        logger.info(
            "Flushing buffered messages - requests={0}, messages={1}".format(
                len(requests), self.message_buffer_size
            )
        )
        super(LoggingKafkaProducer, self)._publish_produce_requests(requests)
        logger.info("All messages published successfully")

    def _reset_message_buffer(self):
        logger.info("Resetting message buffer")
        super(LoggingKafkaProducer, self)._reset_message_buffer()
