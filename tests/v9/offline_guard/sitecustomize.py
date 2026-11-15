import socket


def denied(*args, **kwargs):
    raise RuntimeError("network disabled for offline tests")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
