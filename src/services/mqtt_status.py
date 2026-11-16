"""MQTT connection state, shared without a reference cycle.

``MQTTService`` needs ``SMSService`` (to send and to publish its
metrics); ``SMSService`` only needs to know whether MQTT is connected
(``/api/sms/status`` connections, ``mqtt_connected`` metric). Both get
the same ``MQTTStatus`` at construction: MQTT writes it, SMS reads it.
"""
from dataclasses import dataclass


@dataclass
class MQTTStatus:
    connected: bool = False
