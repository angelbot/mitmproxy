from dataclasses import dataclass
import enum
import ipaddress
import socket
import struct
from typing import Callable, Dict, List, Union

from mitmproxy import dns, platform
from mitmproxy import connection
from mitmproxy.proxy import commands, events, layer
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.utils import expect
from mitmproxy.utils import human


@dataclass
class DnsRequestHook(commands.StartHook):
    """
    A DNS query has been received.
    """
    flow: dns.DNSFlow


@dataclass
class DnsResponseHook(commands.StartHook):
    """
    A DNS response has been received or set.
    """
    flow: dns.DNSFlow


@dataclass
class DnsErrorHook(commands.StartHook):
    """
    A DNS error has occurred.
    """
    flow: dns.DNSFlow


class DnsMode(enum.Enum):
    Simple = "simple"
    Forward = "forward"
    Transparent = "transparent"


class DnsResolveError(Exception):
    def __init__(self, response_code: dns.ResponseCode):
        assert response_code is not dns.ResponseCode.NOERROR
        self.response_code = response_code


class DNSLayer(layer.Layer):
    """
    Layer that handles resolving DNS queries.
    """

    flows: Dict[int, dns.DNSFlow]
    mode: DnsMode

    def __init__(self, context: Context):
        super().__init__(context)
        self.flows = dict()

    @classmethod
    def simple_resolve(cls, questions: List[dns.Question]) -> List[dns.ResourceRecord]:
        answers = []

        def resolve_by_name(family: socket.AddressFamily, ip: Callable[[str], Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]) -> None:
            nonlocal answers, question
            try:
                addrinfos = socket.getaddrinfo(host=question.name, port=0, family=family)
            except socket.gaierror as e:
                if e.errno == socket.EAI_NODATA:
                    raise DnsResolveError(dns.ResponseCode.NXDOMAIN)
                else:
                    # NOTE might fail on Windows for IPv6 queries:
                    # https://stackoverflow.com/questions/66755681/getaddrinfo-c-on-windows-not-handling-ipv6-correctly-returning-error-code-1
                    raise DnsResolveError(dns.ResponseCode.SERVFAIL)
            for addrinfo in addrinfos:
                _, _, _, _, (addr, _) = addrinfo
                answers.append(dns.ResourceRecord(
                    name=question.name,
                    type=question.type,
                    class_=question.class_,
                    ttl=dns.ResourceRecord.DEFAULT_TTL,
                    data=ip(addr).packed,
                ))

        def resolve_by_addr(suffix: str, ip: Callable[[List[str]], Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]) -> bool:
            nonlocal answers, question
            if not question.name.lower().endswith(suffix.lower()):
                return False
            try:
                addr = ip(question.name[0:-len(suffix)].split(".")[::-1])
            except ValueError:
                raise DnsResolveError(dns.ResponseCode.FORMERR)
            try:
                name, _, _ = socket.gethostbyaddr(str(addr))
            except socket.herror:
                raise DnsResolveError(dns.ResponseCode.NXDOMAIN)
            except socket.gaierror:
                raise DnsResolveError(dns.ResponseCode.SERVFAIL)
            answers.append(dns.ResourceRecord(
                name=question.name,
                type=question.type,
                class_=question.class_,
                ttl=dns.ResourceRecord.DEFAULT_TTL,
                data=dns.ResourceRecord.pack_domain_name(name),
            ))
            return True

        for question in questions:
            if question.class_ is not dns.Class.IN:
                raise DnsResolveError(dns.ResponseCode.NOTIMP)
            if question.type is dns.Type.A:
                resolve_by_name(socket.AddressFamily.AF_INET, ipaddress.IPv4Address)
            elif question.type is dns.Type.AAAA:
                resolve_by_name(socket.AddressFamily.AF_INET6, ipaddress.IPv6Address)
            elif question.type is dns.Type.PTR:
                known_family = (
                    resolve_by_addr(".in-addr.arpa", lambda x: ipaddress.IPv4Address(".".join(x)))
                    or
                    resolve_by_addr(".ip6.arpa", lambda x: ipaddress.IPv6Address(bytes.fromhex("".join(x))))
                )
                if not known_family:
                    raise DnsResolveError(dns.ResponseCode.FORMERR)
            else:
                raise DnsResolveError(dns.ResponseCode.NOTIMP)
        return answers

    def handle_request(self, msg: dns.Message) -> layer.CommandGenerator[None]:
        flow = dns.DNSFlow(self.context.client, self.context.server, True)
        flow.request = msg
        if self.mode is DnsMode.Transparent:  # get the query's original destination
            assert platform.original_addr is not None
            socket = yield commands.GetSocket(self.context.client)
            try:
                flow.server_conn = connection.Server(platform.original_addr(socket), protocol=connection.ConnectionProtocol.UDP)
            except Exception as e:
                yield commands.Log(f"Transparent DNS mode failed: {e!r}", level="warn")
                return
        yield DnsRequestHook(flow)  # give hooks a chance to produce a response
        if not flow.response:
            if self.mode is DnsMode.Simple:
                try:
                    if not msg.query:
                        raise DnsResolveError(dns.ResponseCode.REFUSED)  # we received an answer from the _client_
                    if msg.op_code is not dns.OpCode.QUERY:
                        raise DnsResolveError(dns.ResponseCode.NOTIMP)  # inverse queries and others are not supported
                    flow.response = msg.succeed(DNSLayer.simple_resolve(msg.questions))
                except DnsResolveError as e:
                    flow.response = msg.fail(e.response_code)
            else:
                if flow.server_conn.state is connection.ConnectionState.CLOSED:  # we need an upstream connection
                    err = yield commands.OpenConnection(flow.server_conn)
                    if err:
                        flow.error = str(err)
                        yield DnsErrorHook(flow)
                        return  # cannot recover from this
                self.flows[msg.id] = flow
                yield commands.SendData(flow.server_conn, msg.packed)
                return  # we need to wait for the server's response
        yield DnsResponseHook(flow)
        yield commands.SendData(self.context.client, flow.response.packed)

    def handle_response(self, msg: dns.Message, server_conn: connection.Connection) -> layer.CommandGenerator[None]:
        flow = self.flows[msg.id]
        if flow.server_conn is server_conn:
            del self.flows[msg.id]
            flow.response = msg
            yield DnsResponseHook(flow)
            yield commands.SendData(self.context.client, flow.response.packed)
            if self.mode is DnsMode.Transparent:  # always close transparent connections
                yield commands.CloseConnection(flow.server_conn)
        else:
            flow = dns.DNSFlow(self.context.client, server_conn, True)
            flow.response = msg
            flow.error = f"Received response to message {msg.id} sent to {human.format_address(flow.server_conn.address)}."
            yield DnsErrorHook(flow)

    @expect(events.Start)
    def start(self, _) -> layer.CommandGenerator[None]:
        mode: str = self.context.options.dns_mode
        try:
            if mode == DnsMode.Simple.value:
                self.mode = DnsMode.Simple
            elif mode == DnsMode.Transparent.value:
                self.mode = DnsMode.Transparent
            elif mode.startswith(DnsMode.Forward.value):
                self.mode = DnsMode.Forward
                parts = mode[len(DnsMode.Forward.value):].split(":")
                if len(parts) < 2 or len(parts) > 3 or parts[0] != "":
                    raise ValueError(f"Invalid DNS forward mode, expected 'forward:ip[:port]' got '{mode}'.")
                address = (parts[1], int(parts[2]) if len(parts) == 3 else 53)
                self.context.server = connection.Server(address, protocol=connection.ConnectionProtocol.UDP)
            else:
                raise ValueError(f"Invalid DNS mode '{mode}'.")
            self._handle_event = self.query
        except ValueError as e:
            yield commands.Log(f"{str(e)}. Disabling further message handling.", level="error")
            self._handle_event = self.done

    @expect(events.DataReceived, events.ConnectionClosed)
    def query(self, event: events.Event) -> layer.CommandGenerator[None]:
        assert isinstance(event, events.ConnectionEvent)

        if isinstance(event, events.DataReceived):
            from_client = event.connection is self.context.client
            try:
                msg = dns.Message.unpack(event.data)
            except struct.error as e:
                flow = dns.DNSFlow(self.context.client, self.context.server if from_client else event.connection, True)
                flow.error = str(e)
                yield DnsErrorHook(flow)
                return
            if msg.id in self.flows:
                if from_client:  # duplicate ID, remove the old flow with an error and create a new one
                    flow = self.flows[msg.id]
                    del self.flows[msg.id]
                    flow.error = f"Received duplicate request for id {msg.id}."
                    yield DnsErrorHook(flow)
                    yield from self.handle_request(msg)
                else:
                    yield from self.handle_response(msg, event.connection)
            else:
                if from_client:
                    yield from self.handle_request(msg)
                else:
                    flow = dns.DNSFlow(self.context.client, event.connection, True)
                    flow.response = msg
                    flow.error = f"Received response for unknown message {msg.id}."
                    yield DnsErrorHook(flow)

        elif isinstance(event, events.ConnectionClosed):
            pass  # TODO

        else:
            raise AssertionError(f"Unexpected event: {event}")

    @expect(events.DataReceived, events.ConnectionClosed)
    def done(self, _) -> layer.CommandGenerator[None]:
        yield from ()

    _handle_event = start


if __name__ == "__main__":
    # TODO move this into proper test
    msg = dns.Message(
        timestamp=1647775951.981541,
        id=31415,
        query=True,
        op_code=dns.OpCode.QUERY,
        authoritative_answer=False,
        truncation=False,
        recursion_desired=True,
        recursion_available=False,
        reserved=5,
        response_code=dns.ResponseCode.NOERROR,
        questions=[
            dns.Question(
                name="www.aufbauwerk.com",
                type=dns.Type.A,
                class_=dns.Class.IN,
            ),
            # dns.Question(
            #     name="www.google.at",
            #     type=dns.Type.AAAA,
            #     class_=dns.Class.IN,
            # ),
            dns.Question(
                name="8.8.8.8.in-addr.arpa",
                type=dns.Type.PTR,
                class_=dns.Class.IN,
            ),
            dns.Question(
                name="8.8.8.8.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.6.8.4.0.6.8.4.1.0.0.2.ip6.arpa",
                type=dns.Type.PTR,
                class_=dns.Class.IN,
            )
        ],
        answers=[],
        authorities=[],
        additionals=[],
    )
    assert dns.Message.from_state(msg.get_state()) == msg
    copy = dns.Message.unpack(msg.packed)
    copy.timestamp = msg.timestamp
    assert copy == msg
    answers = DNSLayer.simple_resolve(msg.questions)
    answer = msg.succeed(answers)
    assert(dns.Message.from_state(answer.get_state()) == answer)
    copy = dns.Message.unpack(answer.packed)
    copy.timestamp = answer.timestamp
    assert copy == answer
    assert str(answer) == "\r\n".join([
        "www.aufbauwerk.com",
        "8.8.8.8.in-addr.arpa",
        "8.8.8.8.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.6.8.4.0.6.8.4.1.0.0.2.ip6.arpa",
        "217.160.0.146",
        "dns.google",
        "dns.google",
    ])
    compressed = b''.join([
        b'\x04\xd2\x81\x8b\x00\x03\x00\x03\x00\x00\x00\x00\x03www\x06google\x02at\x00\x00\x01\x00\x01\x018\x018\x018\x018',
        b'\x07in-addr\x04arpa\x00\x00\x0c\x00\x01\x018\x018\x018\x018\x010\x010\x010\x010\x010\x010\x010\x010\x010\x010\x010',
        b'\x010\x010\x010\x010\x010\x010\x016\x018\x014\x010\x016\x018\x014\x011\x010\x010\x012\x03ip6\xc0\x2f\x00\x0c\x00',
        b'\x01\x03www\x06google\x02at\x00\x00\x01\x00\x01\x00\x00\x00\x01\x00\x04\xac\xd9\x17c\x018\x018\x018\x018\x07in-addr',
        b'\x04arpa\x00\x00\x0c\x00\x01\x00\x00\x00\x01\x00\x0c\x03dns\x06google\x00\x018\x018\x018\x018\x010\x010\x010\x010',
        b'\x010\x010\x010\x010\x010\x010\x010\x010\x010\x010\x010\x010\x010\x016\x018\x014\x010\x016\x018\x014\x011\x010\x010',
        b'\x012\x03ip6\x04arpa\x00\x00\x0c\x00\x01\x00\x00\x00\x01\x00\x0c\x03dns\x06google\x00'
    ])
    assert dns.Message.unpack(compressed).answers[1].name.endswith(".arpa")
