# -*- coding: utf-8 -*-

# python std lib
from __future__ import with_statement
import re

# rediscluster imports
from rediscluster import StrictRedisCluster
from rediscluster.connection import ClusterConnectionPool
from rediscluster.exceptions import RedisClusterException
from rediscluster.nodemanager import NodeManager
from tests.conftest import _get_client, skip_if_server_version_lt

# 3rd party imports
from mock import patch, Mock
from redis.exceptions import ResponseError
from redis._compat import unicode
import pytest


pytestmark = skip_if_server_version_lt('2.9.0')


def test_representation(r):
    assert re.search('^StrictRedisCluster<[0-9\.\:\,].+>$', str(r))


def test_blocked_strict_redis_args():
    """
    Some arguments should explicitly be blocked because they will not work in a cluster setup
    """
    params = {'startup_nodes': [{'host': '127.0.0.1', 'port': 7000}]}
    c = StrictRedisCluster(**params)
    assert c.connection_pool.connection_kwargs["socket_timeout"] == ClusterConnectionPool.RedisClusterDefaultTimeout

    with pytest.raises(RedisClusterException) as ex:
        _get_client(db=1)
    assert unicode(ex.value).startswith("Argument 'db' is not possible to use in cluster mode")


def test_host_port_startup_node():
    """
    Test that it is possible to use host & port arguments as startup node args
    """
    h = "192.168.0.1"
    p = 7000
    c = StrictRedisCluster(host=h, port=p, init_slot_cache=False)
    assert {"host": h, "port": p} in c.connection_pool.nodes.startup_nodes


def test_empty_startup_nodes(s):
    """
    Test that exception is raised when empty providing empty startup_nodes
    """
    with pytest.raises(RedisClusterException) as ex:
        _get_client(init_slot_cache=False, startup_nodes=[])

    assert unicode(ex.value).startswith("No startup nodes provided"), unicode(ex.value)


def test_blocked_commands(r):
    """
    These commands should be blocked and raise RedisClusterException
    """
    blocked_commands = [
        "CLIENT SETNAME", "SENTINEL GET-MASTER-ADDR-BY-NAME", 'SENTINEL MASTER', 'SENTINEL MASTERS',
        'SENTINEL MONITOR', 'SENTINEL REMOVE', 'SENTINEL SENTINELS', 'SENTINEL SET',
        'SENTINEL SLAVES', 'SHUTDOWN', 'SLAVEOF', 'EVALSHA', 'SCRIPT EXISTS', 'SCRIPT KILL',
        'SCRIPT LOAD', 'MOVE', 'BITOP',
    ]

    for command in blocked_commands:
        try:
            r.execute_command(command)
        except RedisClusterException:
            pass
        else:
            raise AssertionError("'RedisClusterException' not raised for method : {}".format(command))


def test_blocked_transaction(r):
    """
    Method transaction is blocked/NYI and should raise exception on use
    """
    with pytest.raises(RedisClusterException) as ex:
        r.transaction(None)
    assert unicode(ex.value).startswith("method StrictRedisCluster.transaction() is not implemented"), unicode(ex.value)


def test_cluster_of_one_instance():
    """
    Test a cluster that starts with only one redis server and ends up with
    one server.

    There is another redis server joining the cluster, hold slot 0, and
    eventually quit the cluster. The StrictRedisCluster instance may get confused
    when slots mapping and nodes change during the test.
    """
    with patch.object(StrictRedisCluster, 'parse_response') as parse_response_mock:
        with patch.object(NodeManager, 'initialize', autospec=True) as init_mock:
            def side_effect(self, *args, **kwargs):
                def ok_call(self, *args, **kwargs):
                    assert self.port == 7007
                    return "OK"
                parse_response_mock.side_effect = ok_call

                resp = ResponseError()
                resp.args = ('CLUSTERDOWN The cluster is down. Use CLUSTER INFO for more information',)
                resp.message = 'CLUSTERDOWN The cluster is down. Use CLUSTER INFO for more information'
                raise resp

            def side_effect_rebuild_slots_cache(self):
                # make new node cache that points to 7007 instead of 7006
                self.nodes = [{'host': '127.0.0.1', 'server_type': 'master', 'port': 7006, 'name': '127.0.0.1:7006'}]
                self.slots = {}

                for i in range(0, 16383):
                    self.slots[i] = {
                        'host': '127.0.0.1',
                        'server_type': 'master',
                        'port': 7006,
                        'name': '127.0.0.1:7006',
                    }

                # Second call should map all to 7007
                def map_7007(self):
                    self.nodes = [{'host': '127.0.0.1', 'server_type': 'master', 'port': 7007, 'name': '127.0.0.1:7007'}]
                    self.slots = {}

                    for i in range(0, 16383):
                        self.slots[i] = {
                            'host': '127.0.0.1',
                            'server_type': 'master',
                            'port': 7007,
                            'name': '127.0.0.1:7007',
                        }

                # First call should map all to 7006
                init_mock.side_effect = map_7007

            parse_response_mock.side_effect = side_effect
            init_mock.side_effect = side_effect_rebuild_slots_cache

            rc = StrictRedisCluster(host='127.0.0.1', port=7006)
            rc.set("foo", "bar")

            #####
            # Test that CLUSTERDOWN is handled the same way when used via pipeline

            parse_response_mock.side_effect = side_effect
            init_mock.side_effect = side_effect_rebuild_slots_cache

            rc = StrictRedisCluster(host='127.0.0.1', port=7006)
            p = rc.pipeline()
            p.set("bar", "foo")
            p.execute()


def test_moved_exception_handling(r):
    """
    Test that `handle_cluster_command_exception` deals with MOVED
    error correctly.
    """
    resp = ResponseError()
    resp.message = "MOVED 1337 127.0.0.1:7000"
    r.handle_cluster_command_exception(resp)
    assert r.refresh_table_asap is True
    assert r.connection_pool.nodes.slots[1337] == {
        "host": "127.0.0.1",
        "port": 7000,
        "name": "127.0.0.1:7000",
        "server_type": "master",
    }


def test_ask_exception_handling(r):
    """
    Test that `handle_cluster_command_exception` deals with ASK
    error correctly.
    """
    resp = ResponseError()
    resp.message = "ASK 1337 127.0.0.1:7000"
    assert r.handle_cluster_command_exception(resp) == {
        "name": "127.0.0.1:7000",
        "method": "ask",
    }


def test_raise_regular_exception(r):
    """
    If a non redis server exception is passed in it shold be
    raised again.
    """
    e = Exception("foobar")
    with pytest.raises(Exception) as ex:
        r.handle_cluster_command_exception(e)
    assert unicode(ex.value).startswith("foobar")


def test_clusterdown_exception_handling():
    """
    Test that if exception message starts with CLUSTERDOWN it should
    disconnect the connection pool and set refresh_table_asap to True.
    """
    with patch.object(ClusterConnectionPool, 'disconnect') as mock_disconnect:
        with patch.object(ClusterConnectionPool, 'reset') as mock_reset:
            r = StrictRedisCluster(host="127.0.0.1", port=7000)
            i = len(mock_reset.mock_calls)

            assert r.handle_cluster_command_exception(Exception("CLUSTERDOWN")) == {"method": "clusterdown"}
            assert r.refresh_table_asap is True

            mock_disconnect.assert_called_once_with()

            # reset() should only be called once inside `handle_cluster_command_exception`
            assert len(mock_reset.mock_calls) - i == 1


def test_execute_command_errors(r):
    """
    If no command is given to `_determine_nodes` then exception
    should be raised.

    Test that if no key is provided then exception should be raised.
    """
    with pytest.raises(RedisClusterException) as ex:
        r.execute_command()
    assert unicode(ex.value).startswith("Unable to determine command to use")

    with pytest.raises(RedisClusterException) as ex:
        r.execute_command("GET")
    assert unicode(ex.value).startswith("No way to dispatch this command to Redis Cluster. Missing key.")


def test_refresh_table_asap():
    """
    If this variable is set externally, initialize() should be called.
    """
    with patch.object(NodeManager, 'initialize') as mock_initialize:
        mock_initialize.return_value = None

        r = StrictRedisCluster(host="127.0.0.1", port=7000)
        r.connection_pool.nodes.slots[12182] = {
            "host": "127.0.0.1",
            "port": 7002,
            "name": "127.0.0.1:7002",
            "server_type": "master",
        }
        r.refresh_table_asap = True

        i = len(mock_initialize.mock_calls)
        r.execute_command("SET", "foo", "bar")
        assert len(mock_initialize.mock_calls) - i == 1
        assert r.refresh_table_asap is False


def test_ask_redirection():
    """
    Test that the server handles ASK response.

    At first call it should return a ASK ResponseError that will point
    the client to the next server it should talk to.

    Important thing to verify is that it tries to talk to the second node.
    """
    r = StrictRedisCluster(host="127.0.0.1", port=7000)

    m = Mock(autospec=True)

    def ask_redirect_effect(connection, command_name, **options):
        def ok_response(connection, command_name, **options):
            assert connection.host == "127.0.0.1"
            assert connection.port == 7001

            return "MOCK_OK"
        m.side_effect = ok_response
        resp = ResponseError()
        resp.message = "ASK 1337 127.0.0.1:7001"
        raise resp

    m.side_effect = ask_redirect_effect

    r.parse_response = m
    assert r.execute_command("SET", "foo", "bar") == "MOCK_OK"


def test_ask_redirection_pipeline():
    """
    Test that the server handles ASK response when used in pipeline.

    At first call it should return a ASK ResponseError that will point
    the client to the next server it should talk to.

    Important thing to verify is that it tries to talk to the second node.
    """
    r = StrictRedisCluster(host="127.0.0.1", port=7000)
    p = r.pipeline()

    m = Mock(autospec=True)

    def ask_redirect_effect(connection, command_name, **options):
        def ok_response(connection, command_name, **options):
            assert connection.host == "127.0.0.1"
            assert connection.port == 7001

            return "MOCK_OK"
        m.side_effect = ok_response
        resp = ResponseError()
        resp.message = "ASK 12182 127.0.0.1:7001"
        raise resp

    m.side_effect = ask_redirect_effect

    p.parse_response = m
    p.set("foo", "bar")
    assert p.execute() == ["MOCK_OK"]


def test_moved_redirection():
    """
    Test that the client handles MOVED response.

    At first call it should return a MOVED ResponseError that will point
    the client to the next server it should talk to.

    Important thing to verify is that it tries to talk to the second node.
    """
    r = StrictRedisCluster(host="127.0.0.1", port=7000)
    m = Mock(autospec=True)

    def ask_redirect_effect(connection, command_name, **options):
        def ok_response(connection, command_name, **options):
            assert connection.host == "127.0.0.1"
            assert connection.port == 7002

            return "MOCK_OK"
        m.side_effect = ok_response
        resp = ResponseError()
        resp.message = "MOVED 12182 127.0.0.1:7002"
        raise resp

    m.side_effect = ask_redirect_effect

    r.parse_response = m
    assert r.set("foo", "bar") == "MOCK_OK"


def test_moved_redirection_pipeline():
    """
    Test that the server handles MOVED response when used in pipeline.

    At first call it should return a MOVED ResponseError that will point
    the client to the next server it should talk to.

    Important thing to verify is that it tries to talk to the second node.
    """
    r = StrictRedisCluster(host="127.0.0.1", port=7000)
    p = r.pipeline()

    m = Mock(autospec=True)

    def moved_redirect_effect(connection, command_name, **options):
        def ok_response(connection, command_name, **options):
            assert connection.host == "127.0.0.1"
            assert connection.port == 7002

            return "MOCK_OK"
        m.side_effect = ok_response
        resp = ResponseError()
        resp.message = "MOVED 12182 127.0.0.1:7002"
        raise resp

    m.side_effect = moved_redirect_effect

    p.parse_response = m
    p.set("foo", "bar")
    assert p.execute() == ["MOCK_OK"]
