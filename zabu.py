from zavudev import Zavudev

client = Zavudev(
    api_key="zv_live_45583328e9346c901f2c5d4c1f53c1c01c3484cd664231e1"
)

message_response = client.messages.send(
    to="+5213338449486",
    text="Hello from Zavu!",
)

print(message_response.message)