"""Bounded, explicitly referenced document input for the proposal Builder."""
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def reference_roots(worktree_path):
    """Folders a referenced document may live in: the worktree, the project it was cut from, and the folders the
    operator listed under safety.reference_roots in config.yaml."""
    from core.env_utils import root_config, run_capture
    roots = [Path(worktree_path).resolve()]
    try:
        res = run_capture(['git', 'rev-parse', '--git-common-dir'], cwd=str(roots[0]), timeout=10)
        common = Path(res.stdout.strip())
        if res.returncode == 0 and res.stdout.strip():
            common = (common if common.is_absolute() else roots[0] / common).resolve()
            if common.name == '.git':
                roots.append(common.parent)
    except Exception:
        pass
    for extra in (root_config().get('safety') or {}).get('reference_roots') or []:
        try:
            roots.append(Path(str(extra)).resolve())
        except OSError:
            continue
    return roots


def _inside(path, roots):
    target = path.resolve()
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def referenced_documents(order, roots=None):
    """roots=None keeps the unrestricted behaviour for direct callers; the Builder always passes reference_roots()
    (2026-11-20, public review: any absolute .md/.txt/.docx named in an ORDER was read and sent to the provider)."""
    text = '\n'.join([order.goal, order.context or '', *order.acceptance_criteria])
    paths = list(getattr(order, 'reference_paths', []))
    # A drive letter that starts a token (not the 'p' of 'http://', not a URL scheme '://') begins a document path.
    # A path never contains = * ? ' ( ): a sentence such as "E:\proj = a game ... see notes.md" is prose, not one path.
    paths += re.findall(r'(?<![A-Za-z0-9])[A-Za-z]:[/\\](?![/\\])[^\r\n"<>|`=*?\x27()]*?\.(?:docx|md|txt)(?=[\s"\x27`)\],;:]|$)', text, re.I)
    documents = {}
    for value in dict.fromkeys(paths):
        path = Path(value)
        try:
            if not path.is_absolute() or path.is_symlink():
                raise ValueError('Reference must be an absolute regular file')
            if roots is not None and not _inside(path, roots):
                raise ValueError('outside the project and safety.reference_roots; add its folder to '
                                 'safety.reference_roots in config.yaml to allow it')
            if path.stat().st_size > 10_000_000:
                raise ValueError('Reference exceeds 10 MB')
            if path.suffix.lower() == '.docx':
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo('word/document.xml')
                    if info.file_size > 5_000_000:
                        raise ValueError('Document XML exceeds limit')
                    root = ElementTree.fromstring(archive.read(info))
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                content = '\n'.join(''.join(p.itertext()) for p in root.findall('.//w:p', ns))
            elif path.suffix.lower() in {'.txt', '.md'}:
                content = path.read_text(encoding='utf-8')
            else:
                raise ValueError('Unsupported reference format')
            if len(content) > 100_000 or sum(len(v) for v in documents.values()) + len(content) > 200_000:
                raise ValueError('Reference text exceeds context budget')
            documents[str(path)] = content
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError(f'REFERENCE_INPUT_UNAVAILABLE: {value}: {exc}') from exc
    return documents
