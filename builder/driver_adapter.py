import ast
from pathlib import Path

from builder.downloads import verify


def nodes(code):
    return ast.parse(code).body


def command_id(statement):
    if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if isinstance(call.func, ast.Attribute) and call.func.attr == '__send_cmd' and call.args and isinstance(call.args[0], ast.Constant):
        return call.args[0].value
    return None


def adapt_driver(source, target, expected_sha):
    verify(source, expected_sha)
    tree = ast.parse(Path(source).read_text())
    scope = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'Hantek1008Raw')
    methods = {item.name: item for item in scope.body if isinstance(item, ast.FunctionDef)}
    init = methods['__init__']
    index = next(i for i, item in enumerate(init.body) if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Attribute) and item.target.attr == '_zero_offsets')
    init.body[index + 1:index + 1] = nodes('self._device_response_blocks: Dict[int, bytes] = {}\nself.device_metadata = {}\nself.cancel_requested = lambda: False')
    connect = methods['connect']
    index = next(i for i, item in enumerate(connect.body) if isinstance(item, ast.Expr) and isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Attribute) and item.value.func.attr == 'set_configuration')
    connect.body[index:index] = nodes('self._dev.default_timeout = 1000')
    ready = methods['__send_a55a_command']
    ready.args.defaults[0] = ast.Constant(100)
    next(item for item in ready.body if isinstance(item, ast.For)).body[0:0] = nodes('if self.cancel_requested():\n    raise InterruptedError("Acquisition stopped")')
    result = []
    captured = []
    for item in methods['_init1'].body:
        result.append(item)
        code = command_id(item)
        if code in (0xf7, 0xf8, 0xfa):
            result += nodes(f'self._device_response_blocks[{code}] = response')
            captured.append(code)
    if captured != [0xf7, 0xf8, 0xfa]:
        raise ValueError('Unexpected driver calibration layout')
    methods['_init1'].body = result
    result, last, replaced = [], None, 0
    for item in methods['_init3'].body:
        code = command_id(item)
        if code is not None:
            last = code
        if isinstance(item, ast.Assert) and last in (0xe5, 0xe9, 0xf7, 0xf8, 0xfa):
            if last in (0xe5, 0xe9):
                key = 'calibration_temperature_raw' if last == 0xe5 else 'arm_version_raw'
                result += nodes(f'self.device_metadata[{key!r}] = int.from_bytes(response, "little")')
            else:
                result += nodes(f'if response != self._device_response_blocks.get({last}):\n    raise RuntimeError("Device response block {last:02X} changed during initialization")')
            last = None
            replaced += 1
        else:
            result.append(item)
    if replaced != 5:
        raise ValueError('Unexpected driver response checks')
    methods['_init3'].body = result
    burst = nodes('''
def configure_burst_window(self, slot_us: float) -> None:
    count = len(self.__active_channels)
    assert count in (1, 2, 4, 6, 8)
    position = 2 * ((4000 // count) * 50 // 100)
    delay_us = int(2000 * slot_us) + 1
    parameter = position.to_bytes(2, 'big') + delay_us.to_bytes(3, 'big') * 2
    self.__send_cmd(0xac, parameter=parameter)
''')[0]
    scope.body.insert(scope.body.index(methods['request_samples_burst_mode']), burst)
    modified = 0
    for item in ast.walk(methods['request_samples_roll_mode']):
        if isinstance(item, ast.While) and (ast.dump(item.test) == ast.dump(ast.Constant(True)) or
                                          ast.dump(item.test) == ast.dump(ast.parse('ready_data_length == 0', mode='eval').body)):
            item.body[0:0] = nodes('if self.cancel_requested():\n    return')
            modified += 1
    if modified != 2:
        raise ValueError('Unexpected rolling acquisition loops')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ast.unparse(ast.fix_missing_locations(tree)) + '\n')
