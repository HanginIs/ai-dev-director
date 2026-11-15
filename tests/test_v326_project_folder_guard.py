"""A folder that is not a project must not be turned into a git repository.

Two missions ("check this app") pointed at ~/Applications/Chrome Apps.localized and at ~/Downloads. The Director ran
`git init` there and copied the folder's files into .director_worktrees. Both had to be cleaned up by hand."""
from pathlib import Path

import pytest

from core import bootstrap
from core.bootstrap import not_a_project_folder, prepare_project, snapshot


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / 'home'
    h.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: h))
    return h


def _untouched(folder):
    return not (folder / '.git').exists() and not (folder / '.director_worktrees').exists()


@pytest.mark.parametrize('name', ['Downloads', 'Desktop', 'Documents'])
def test_personal_folder_itself_is_refused_and_left_alone(home, name):
    folder = home / name
    folder.mkdir()
    (folder / 'Diary Book Kit.html').write_text('<html></html>')     # a source file does not make it a project
    with pytest.raises(ValueError) as e:
        prepare_project(folder)
    assert '프로젝트 폴더로 쓸 수 없습니다' in str(e.value) and '개인 파일 폴더' in str(e.value)
    assert _untouched(folder)


def test_folder_name_case_does_not_matter(home):
    (home / 'Downloads').mkdir()
    assert not_a_project_folder(home / 'downloads')


def test_project_inside_documents_is_fine(home):
    project = home / 'Documents' / 'my-app'
    assert prepare_project(project) == str(project.resolve())
    assert (project / '.git').exists()


def test_anything_under_applications_is_refused(home):
    folder = home / 'Applications' / 'Chrome Apps.localized'
    folder.mkdir(parents=True)
    with pytest.raises(ValueError) as e:
        prepare_project(folder)
    assert '설치되는 곳' in str(e.value)
    assert _untouched(folder)


def test_missing_folder_under_applications_is_not_created(home):
    folder = home / 'Applications' / 'new-thing'
    with pytest.raises(ValueError):
        prepare_project(folder)
    assert not folder.exists()


def test_folder_holding_an_app_bundle_is_refused(tmp_path, home):
    folder = tmp_path / 'tools'
    (folder / 'Diary Book.app' / 'Contents').mkdir(parents=True)
    with pytest.raises(ValueError) as e:
        prepare_project(folder)
    assert 'Diary Book.app' in str(e.value)
    assert _untouched(folder)


def test_inside_an_app_bundle_is_refused(tmp_path, home):
    folder = tmp_path / 'Some.app' / 'Contents' / 'Resources'
    folder.mkdir(parents=True)
    with pytest.raises(ValueError) as e:
        prepare_project(folder)
    assert '앱 번들' in str(e.value)


def test_a_file_named_like_an_app_is_not_an_app_bundle(tmp_path, home):
    folder = tmp_path / 'proj'
    folder.mkdir()
    (folder / 'notes.app').write_text('not a bundle')
    assert prepare_project(folder) == str(folder.resolve())


def test_folder_the_user_made_a_repository_is_their_decision(home):
    folder = home / 'Downloads'
    folder.mkdir()
    bootstrap.git(folder, 'init')
    assert prepare_project(folder) == str(folder.resolve())


def test_snapshot_refuses_too(home):
    folder = home / 'Downloads'
    folder.mkdir()
    with pytest.raises(ValueError):
        snapshot(folder)
    assert _untouched(folder)


def test_ordinary_new_folder_still_works(tmp_path, home):
    assert not_a_project_folder(tmp_path / 'brand-new') is None
    project = Path(prepare_project(tmp_path / 'brand-new'))
    assert bootstrap.git(project, 'rev-parse', 'HEAD')
