"""迁移期状态封装验收；退役工具删除时一起移除，不运行算法。"""

from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
from types import SimpleNamespace

import pytest

from harness import attachment_retirement as retirement
from scheduler.blackbox_state import StateBinding, StateSession, _encode, _decode


@pytest.mark.parametrize('case', ['new', 'identical', 'different', 'runtime_drift', 'missing'])
def test_state_conversion_preserves_input_payload_and_never_overwrites_source(tmp_path, monkeypatch, case):
    root = tmp_path / 'states'
    parent = root / 'scheme'
    parent.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    identity = {'script_sha256': 'a' * 64, 'runtime_sha256': 'b' * 64}
    inputs = {'schema': 'data-bridge-v1', 'generation_id': 'older-generation', 'files': {'daily.csv': 'c' * 64}}
    payload = b'unchanged derived arrays'
    import hashlib
    header = {'identity': identity | {'scheme_id': 'scheme', 'scheme_version': 'old'},
              'input': inputs, 'payload_sha256': hashlib.sha256(payload).hexdigest()}
    if case == 'runtime_drift':
        header['identity']['runtime_sha256'] = 'd' * 64
    before = _encode(header, payload)
    source = parent / 'old.state'
    if case != 'missing':
        source.write_bytes(before)
    destination = parent / 'new.state'
    expected = _encode(header | {'identity': identity | {'scheme_id': 'scheme', 'scheme_version': 'new'}}, payload)
    if case == 'identical':
        destination.write_bytes(expected)
    elif case == 'different':
        destination.write_bytes(b'foreign state')
    old = {'scheme': SimpleNamespace(scheme_version='old')}
    new = {'scheme': SimpleNamespace(scheme_version='new', incremental_state=True, blackbox_metadata=None, delivery_script=None)}
    snapshot = SimpleNamespace(generation_id='current-generation')
    monkeypatch.setattr(retirement, 'IDS', ['scheme'])
    monkeypatch.setattr(retirement, '_load_runtime_profile', lambda path: None)
    monkeypatch.setattr(retirement, 'compose_blackbox_input_bundle', lambda *args, **kwargs: None)
    monkeypatch.setattr(retirement, 'open_blackbox_runtime_view', lambda bundle: nullcontext(
        SimpleNamespace(data_dir=tmp_path, bundle=SimpleNamespace(combined_snapshot_id='current-snapshot'))))
    monkeypatch.setattr(retirement, 'state_binding_for_scheme', lambda cfg, **kwargs: StateBinding(
        scheme_id='scheme', scheme_version='new', generation_id='current-generation', root=root, rebuild=True))
    monkeypatch.setattr(retirement, '_state_session', lambda binding, work, *args: StateSession(
        binding, work_dir=work, identity=identity, input_identity={'schema': 'data-bridge-v1'}))
    if case in ('different', 'runtime_drift', 'missing'):
        with pytest.raises((ValueError, RuntimeError, FileNotFoundError)):
            retirement._prepare_states(None, old, new, snapshot)
        if case == 'different':
            assert destination.read_bytes() == b'foreign state'
        else:
            assert not destination.exists()
    else:
        result = retirement._prepare_states(None, old, new, snapshot)
        actual_header, actual_payload = _decode(destination.read_bytes())
        assert actual_header['input'] == inputs
        assert actual_payload == payload
        assert destination.read_bytes() == expected
        assert result['scheme']['input_preserved'] is True
    if case != 'missing':
        assert source.read_bytes() == before


def test_mutation_requires_approval_before_database_connection(monkeypatch):
    def forbidden():
        pytest.fail('must reject before database connection')
    monkeypatch.setattr(retirement.repo, 'create_engine_from_env', forbidden)
    with pytest.raises(SystemExit):
        retirement.main(['cutover', '--project-root', '/candidate', '--reference-project-root', '/source',
            '--expected-archive-sha256', 'a' * 64, '--expected-source-archive-sha256', 'b' * 64,
            '--expected-database-name', 'isolated', '--expected-server-uuid', 'test'])


@pytest.mark.parametrize('action,current_label,advanced,accepted', [
    ('cutover', 'source', False, False),
    ('rollback', 'candidate', True, True),
    ('cutover', 'candidate', True, False),
    ('cutover', 'candidate', False, True),
    ('preflight', 'source', False, True),
])
def test_capture_current_release_and_advanced_state_boundary(
    tmp_path, monkeypatch, action, current_label, advanced, accepted,
):
    """只模拟控制面读取；状态封装与摘要为临时文件真实字节，不运行算法。"""
    source = tmp_path / 'source'
    candidate = tmp_path / 'candidate'
    source.mkdir(); candidate.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(source if current_label == 'source' else candidate, target_is_directory=True)
    runtime = tmp_path / 'runtime'
    state_dir = runtime / 'blackbox-state' / 'scheme'
    state_dir.mkdir(parents=True)
    source_payload = b'original historical arrays'
    header = {'identity': {'scheme_id': 'scheme', 'scheme_version': 'old', 'runtime_sha256': 'e' * 64},
              'input': {'generation_id': 'original-generation'},
              'payload_sha256': hashlib.sha256(source_payload).hexdigest()}
    source_bytes = _encode(header, source_payload)
    (state_dir / 'old.state').write_bytes(source_bytes)
    new_payload = source_payload + b' legitimately advanced suffix' if advanced else source_payload
    candidate_header = header | {'identity': header['identity'] | {'scheme_version': 'new'},
                                  'payload_sha256': hashlib.sha256(new_payload).hexdigest()}
    if advanced:
        candidate_header['input'] = {'generation_id': 'next-generation'}
    candidate_bytes = _encode(candidate_header, new_payload)
    (state_dir / 'new.state').write_bytes(candidate_bytes)

    @dataclass(frozen=True)
    class Config:
        scheme_version: str
        incremental_state: bool = True
        environment_fingerprint: str | None = None
        data_snapshot_id: str | None = None

    monkeypatch.setenv('BFL_DEPLOYMENT_TARGET', 'aliyun-gray')
    monkeypatch.setenv('BFL_RUNTIME_ROOT', str(runtime))
    monkeypatch.setattr(retirement, 'IDS', ['scheme'])
    monkeypatch.setattr(retirement, 'ROOT', candidate)
    monkeypatch.setattr(retirement, 'CURRENT', current)
    monkeypatch.setattr(retirement, '_verified_install', lambda path: {
        'archive_sha256': 'a' * 64 if path == candidate else 'b' * 64})
    monkeypatch.setattr(retirement, '_fence', lambda: {'fenced': True})
    monkeypatch.setattr(retirement, '_capture_installed_locale', lambda *a, **k: {'LANG': 'en_US.UTF-8'})
    monkeypatch.setattr(retirement, '_temporary_alias_bases', lambda: ['scheme'])
    monkeypatch.setattr(retirement, '_assert_no_temporary_writer', lambda *a: {'checked': True})
    monkeypatch.setattr(retirement, '_ready_input', lambda: (SimpleNamespace(), {'current_input': True}))
    monkeypatch.setattr(retirement, 'load_environment_fingerprint', lambda *a, **k: 'e' * 64)
    monkeypatch.setattr(retirement, 'load_scheme_config', lambda path: Config('old' if source in path.parents else 'new'))
    monkeypatch.setattr(retirement.repo, '_read_scheme_version_conn', lambda *a, **k: {
        'environment_fingerprint': 'e' * 64, 'data_snapshot_id': 'original-snapshot'})
    reads = []
    def read_states(project, configs, snapshot):
        # 替身只替换公共校验入口；真实解码不同版本各自的封装，不能混用旧 payload。
        result = {}
        for key, cfg in configs.items():
            raw = (state_dir / f'{cfg.scheme_version}.state').read_bytes()
            actual_header, payload = _decode(raw)
            assert actual_header['identity']['scheme_version'] == cfg.scheme_version
            assert actual_header['identity']['runtime_sha256'] == 'e' * 64
            assert actual_header['payload_sha256'] == hashlib.sha256(payload).hexdigest()
            result[key] = {'sha256': hashlib.sha256(raw).hexdigest(), 'input': actual_header['input']}
            reads.append(cfg.scheme_version)
        return result
    monkeypatch.setattr(retirement, '_read_candidate_states', read_states)
    args = SimpleNamespace(action=action, operation='cutover', project_root=candidate,
        reference_project_root=source, expected_archive_sha256='a' * 64,
        expected_source_archive_sha256='b' * 64)
    engine = SimpleNamespace(connect=lambda: nullcontext(object()))
    if accepted:
        _, _, _, control = retirement._capture(args, engine)
        assert control['state_ready'] is True
        assert control['source_states']['scheme']['sha256'] == hashlib.sha256(source_bytes).hexdigest()
        assert control['candidate_states']['scheme']['sha256'] == hashlib.sha256(candidate_bytes).hexdigest()
        assert reads == ['old', 'new']
    else:
        with pytest.raises((RuntimeError, ValueError)):
            retirement._capture(args, engine)
    assert (state_dir / 'old.state').read_bytes() == source_bytes
    assert (state_dir / 'new.state').read_bytes() == candidate_bytes
