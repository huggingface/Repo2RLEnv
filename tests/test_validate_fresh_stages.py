from repo2rlenv.pipelines import pr_runtime_validate as validate

_STARTED = []


class _Sandbox:
    def __init__(self, name, platform="linux/amd64"):
        self.name, self.platform = name, platform
        self.container_id = name
        self.committed, self.cleaned = [], False

    def commit(self, tag, *, message=""):
        self.committed.append(tag)
        return tag

    def cleanup(self):
        self.cleaned = True


def _fake_start(image, repo_dir, *, platform):
    sandbox = _Sandbox(f"stage{len(_STARTED)}", platform)
    sandbox.image = image
    _STARTED.append(sandbox)
    return sandbox


def test_shared_mode_runs_every_stage_in_the_sandbox():
    anchor = _Sandbox("anchor")
    host = validate._StageHost(anchor, fresh=False)
    assert host.stage() is anchor and host.stage() is anchor
    assert host.reader() is anchor
    host.close()
    assert anchor.committed == [] and not anchor.cleaned


def test_fresh_mode_snapshots_once_and_gives_each_stage_its_own_container(monkeypatch):
    _STARTED.clear()
    monkeypatch.setattr(validate.DockerSandbox, "start", staticmethod(_fake_start))
    docker = []
    monkeypatch.setattr(validate.subprocess, "run", lambda args, **kwargs: docker.append(args[1:]))
    anchor = _Sandbox("anchor", platform="linux/arm64")
    host = validate._StageHost(anchor, fresh=True)
    first, second = host.stage(), host.stage()
    assert first is not second and first is not anchor
    assert len(anchor.committed) == 1  # one snapshot per candidate
    assert {s.image for s in (first, second)} == set(anchor.committed)
    assert first.platform == "linux/arm64"
    assert first.cleaned and not second.cleaned  # the previous stage is discarded
    assert host.reader() is second  # mutants read the latest stage's tree
    host.close()
    assert second.cleaned and not anchor.cleaned
    # Each stage container is killed before removal; the snapshot image is removed.
    assert docker == [
        ["kill", first.container_id],
        ["kill", second.container_id],
        ["image", "rm", "-f", anchor.committed[0]],
    ]
