import json
import time


def ready_then_hang(connection, config, identity, state):
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    connection.recv_bytes()
    time.sleep(60)


def ready_then_echo(connection, config, identity, state):
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    command = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({**command, 'type': 'session',
                                     'snapshot_sequence': 1, 'value': {'state': {}}}).encode())
    connection.send_bytes(json.dumps({**command, 'type': 'result',
                                     'snapshot_sequence': 1, 'value': {'ok': True}}).encode())
    connection.recv_bytes()


def ready_then_wrong_attempt(connection, config, identity, state):
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    command = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({**command, 'type': 'result',
                                     'attempt_id': 'wrong', 'value': {'ok': True}}).encode())
    connection.recv_bytes()


def ready_then_unsaved_snapshot(connection, config, identity, state):
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    command = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({**command, 'type': 'result',
                                     'snapshot_sequence': 1, 'value': {'ok': True}}).encode())
    connection.recv_bytes()


def ready_then_auth_checkpoint_failure(connection, config, identity, state):
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    command = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({**command, 'type': 'auth_checkpoint',
        'value': {'state': {'storage_state': {'cookies': []}}, 'confirmed': False}}).encode())
    connection.send_bytes(json.dumps({**command, 'type': 'failure', 'code': 'auth_timeout',
                                     'message': 'not confirmed'}).encode())
    connection.recv_bytes()


def ready_then_stale_snapshot(connection, config, identity, state):
    connection.send_bytes(json.dumps({'type': 'ready', **config['envelope']}).encode())
    command = json.loads(connection.recv_bytes())
    if config.get('_prime_session'):
        connection.send_bytes(json.dumps({**command, 'type': 'session',
            'snapshot_sequence': 1, 'value': {'state': {}}}).encode())
        connection.send_bytes(json.dumps({**command, 'type': 'result',
            'snapshot_sequence': 1, 'value': {'ok': True}}).encode())
        command = json.loads(connection.recv_bytes())
    connection.send_bytes(json.dumps({**command, 'type': 'result',
        'snapshot_sequence': 1 if config.get('_prime_session') else 0, 'value': {'ok': True}}).encode())
    connection.recv_bytes()
