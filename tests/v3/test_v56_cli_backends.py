from pathlib import Path
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dag_agent.backends import create_backend, normalize_backend
from dag_agent.zcode_backend import ZCodeCliBackend
from dag_agent.daemon import RunManager
from dag_agent.repository import Repository
from dag_agent.graph import Graph

SHIM = Path(__file__).with_name("fake_zcode_cli.py")
CHECKER = Path(__file__).resolve().parents[2] / "dag_agent/checkers/check_file_contains.py"
BRIEF = "【输入】无【交付清单】hello.txt【接口签名】文本文件【样例】DAG-OK【自测入口】检查hello.txt"


def options(tmp_path, timeout=5):
    cfg = tmp_path / "provider.json"
    cfg.write_text(json.dumps({"schemaVersion": 1,
        "config": {"defaultModelSelection": {"providerId": "test", "modelId": "test-model"}},
        "test_secret": "dummy-sensitive-marker"}), encoding="utf8")
    return {"cli_path": str(SHIM), "provider_config": str(cfg), "timeout": timeout}


def test_selection_is_explicit_and_rejects_secrets():
    assert normalize_backend()[0] == "claude-cli"
    with pytest.raises(ValueError):
        create_backend("missing")
    with pytest.raises(ValueError):
        normalize_backend("zcode-cli", {"api_key": "never-persist"})
    with pytest.raises(ValueError):
        normalize_backend("zcode-cli", {"timeout": float("nan")})


def test_zcode_json_receipt_isolated_parallel_and_redacted(tmp_path):
    be = ZCodeCliBackend(**options(tmp_path))
    assert be.available()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        rs = list(pool.map(lambda n: be.run(BRIEF + " TEST_SECRET", tmp_path / n, "check"), ["a", "b"]))
    homes=[]
    for n, r in zip(["a", "b"],rs):
        assert r["ok"] and r["resources"]["tokens"] == 10
        assert r["resources"]["cost"]["basis"] == "unknown"
        assert r["resources"]["session_id"] == "sess-" + n
        p=json.loads((tmp_path/n/"process.json").read_text())
        homes.append(p["home"])
        assert not Path(p["home"]).exists()
        assert "--continue" not in p["args"] and "--resume" not in p["args"]
        assert "dummy-sensitive-marker" not in (tmp_path/n/".dag_session_logs/zcode-stderr.log").read_text()
    assert homes[0] != homes[1]


@pytest.mark.parametrize("marker", ["TEST_GARBAGE", "TEST_NONZERO"])
def test_bad_cli_result_never_success(tmp_path, marker):
    r=ZCodeCliBackend(**options(tmp_path)).run(marker,tmp_path/'a','check')
    assert not r['ok']


def test_pre_cancel_and_running_cancel(tmp_path):
    be=ZCodeCliBackend(**options(tmp_path))
    ev=threading.Event();ev.set()
    assert be.run('TEST_SLEEP',tmp_path/'pre','check',cancel_event=ev)['resources']['cancelled']
    assert not (tmp_path/'pre/process.json').exists()
    ev.clear()
    with ThreadPoolExecutor(max_workers=1) as p:
        f=p.submit(be.run,'TEST_SLEEP',tmp_path/'running','check',cancel_event=ev)
        until=time.monotonic()+5
        while not (tmp_path/'running/process.json').exists() and time.monotonic()<until:time.sleep(.02)
        assert (tmp_path/'running/process.json').exists()
        ev.set();r=f.result(timeout=12)
    assert not r['ok'] and r['resources']['cancelled'] and r['resources']['exit_code'] is not None


def test_timeout_releases_process(tmp_path):
    r=ZCodeCliBackend(**options(tmp_path,timeout=.3)).run('TEST_SLEEP',tmp_path/'t','check')
    assert not r['ok'] and r['resources']['timeout'] and r['resources']['exit_code'] is not None


def mission(tmp_path):
    home=tmp_path/'home';home.mkdir()
    repo=Repository(home/'mission.db');g=Graph(repo)
    cmd=f'python "{CHECKER}" hello.txt DAG-OK'
    mid,root=g.create_mission('switch-test',brief=BRIEF,root_check=cmd,root_declared_artifacts=('hello.txt',))
    return home,repo,g,mid,root


def test_real_manager_zcode_graph_closes_and_charges_unknown(tmp_path):
    opts=options(tmp_path);home,repo,g,mid,root=mission(tmp_path)
    cmd=f'python "{CHECKER}" hello.txt DAG-OK'
    for n in ['A','B']:g.add_node(mid,n,BRIEF,parent_id=root,check_cmd=cmd,declared_artifacts=('hello.txt',))
    mgr=RunManager(home)
    result=mgr.run_blocking(mid,timeout=30,real=True,backend_name='zcode-cli',backend_options=opts,project=False,max_retries=0,timeout_s=25)
    assert result['mission_status']=='done'
    attempts=repo._conn.execute('SELECT backend,resources_json FROM attempts').fetchall()
    assert len(attempts)==3 and all(a['backend']=='zcode-cli' for a in attempts)
    assert all(json.loads(a['resources_json'])['cost']['basis']=='unknown' for a in attempts)
    mgr.cleanup_finished(keep=0);repo.close()


def test_reboot_keeps_choice_and_explicit_start_can_switch(tmp_path):
    opts=options(tmp_path);home,repo,g,mid,root=mission(tmp_path)
    m=RunManager(home)
    m.run_blocking(mid,timeout=10,real=True,backend_name='zcode-cli',backend_options=opts,max_calls=0,project=False)
    m.cleanup_finished(keep=0)
    m2=RunManager(home)
    res=m2.kick(mid)
    assert res['ok'] and m2._runs[mid]['scheduler'].backend.name=='zcode-cli'
    m2._runs[mid]['thread'].join(5)
    m2.cleanup_finished(keep=0)
    # real=False ensures changing the selected CLI cannot spend money.
    r=m2.run_blocking(mid,timeout=10,real=False,backend_name='claude-cli',max_calls=0,project=False)
    params=json.loads(repo._conn.execute('SELECT params_json FROM run_control WHERE id=1').fetchone()[0])
    assert params['backend_name']=='claude-cli'
    m2.cleanup_finished(keep=0);repo.close()


def test_unknown_backend_rejected_before_attempt(tmp_path):
    home,repo,g,mid,root=mission(tmp_path)
    with pytest.raises(ValueError):RunManager(home).start(mid,backend_name='typo')
    assert repo._conn.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
    repo.close()
