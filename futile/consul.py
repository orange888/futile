from functools import lru_cache
from consul import Consul

from futile.net import get_local_ip


def lookup_service(service_name):
    consul = Consul()
    _, services = consul.catalog.service(service_name)
    endpoints = [(s["Address"], s["ServicePort"]) for s in services]
    return endpoints


def lookup_kv(key, default=None):
    consul = Consul()
    _, data = consul.kv.get(key)
    return data.get("Value", default)


def register_service(service_name: str, *, address: str = None, port: int = None):
    consul = Consul()
    if address is None:
        address = get_local_ip()
    consul.agent.service.register(service_name, address=address, port=port)


def deregister_service(service_name: str):
    consul = Consul()
    consul.agent.service.deregister(service_name)
