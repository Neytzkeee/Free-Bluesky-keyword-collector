"""Collect public Bluesky posts and replies by keyword."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE_URL = "https://api.bsky.app/xrpc"
SEARCH_API_URL = f"{API_BASE_URL}/app.bsky.feed.searchPosts"
THREAD_API_URL = f"{API_BASE_URL}/app.bsky.feed.getPostThread"
DEFAULT_KEYWORDS_FILE = Path(__file__).with_name("keywords.txt")
DEFAULT_OUTPUT_FILE = Path(__file__).with_name("bluesky_posts.csv")
DEFAULT_COMMENTS_FILE = Path(__file__).with_name("bluesky_comentarios.csv")
DEFAULT_STATE_FILE = Path(__file__).with_name("bluesky_estado.json")


def normalize(value: str) -> str:
    """Normalize text for case- and accent-insensitive matching."""
    decomposed = unicodedata.normalize("NFD", value.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def read_keywords(path: Path) -> list[str]:
    """Read one keyword per line, ignoring blank lines and comments."""
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de keywords não encontrado: {path}")

    keywords: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        keyword = raw_line.strip()
        if not keyword or keyword.startswith("#"):
            continue
        key = normalize(keyword)
        if key not in seen:
            keywords.append(keyword)
            seen.add(key)
    return keywords


def fetch_page(
    keyword: str,
    limit: int,
    cursor: str | None,
    timeout: int,
) -> dict[str, Any]:
    params: list[tuple[str, str]] = [
        ("q", keyword),
        ("limit", str(limit)),
        ("sort", "latest"),
    ]
    if cursor:
        params.append(("cursor", cursor))
    # This endpoint has no language parameter, so filtering happens locally.

    request = Request(
        f"{SEARCH_API_URL}?{urlencode(params)}",
        headers={"User-Agent": "ElectionsResearchCollector/1.0 (public Bluesky research)"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_thread(uri: str, depth: int, timeout: int) -> dict[str, Any]:
    """Fetch public replies below a post."""
    params = urlencode(
        {
            "uri": uri,
            "depth": depth,
            "parentHeight": 0,
        }
    )
    request = Request(
        f"{THREAD_API_URL}?{params}",
        headers={"User-Agent": "ElectionsResearchCollector/1.0 (public Bluesky research)"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_url(post: dict[str, Any]) -> str:
    uri = post.get("uri", "")
    handle = post.get("author", {}).get("handle", "")
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    if handle and rkey:
        return f"https://bsky.app/profile/{handle}/post/{rkey}"
    return ""


def matching_keywords(text: str, keywords: list[str]) -> list[str]:
    normalized_text = normalize(text)
    return [keyword for keyword in keywords if normalize(keyword) in normalized_text]


def matches_language(post: dict[str, Any], language: str | None) -> bool:
    """Accept the requested language and posts with no language metadata."""
    if not language:
        return True
    languages = post.get("record", {}).get("langs", [])
    return not languages or language in languages


def collect(
    keywords: list[str],
    limit_per_keyword: int,
    max_pages: int,
    language: str | None,
    delay: float,
    timeout: int,
) -> OrderedDict[str, dict[str, Any]]:
    collected: OrderedDict[str, dict[str, Any]] = OrderedDict()

    for position, keyword in enumerate(keywords, start=1):
        print(f"[{position}/{len(keywords)}] Buscando: {keyword}")
        cursor: str | None = None

        for page_number in range(max_pages):
            try:
                payload = fetch_page(keyword, limit_per_keyword, cursor, timeout)
            except HTTPError as error:
                print(f"  Ignorado: API respondeu HTTP {error.code}.")
                break
            except (URLError, TimeoutError) as error:
                print(f"  Ignorado: não foi possível conectar à API ({error}).")
                break

            posts = payload.get("posts", [])
            for post in posts:
                if not matches_language(post, language):
                    continue
                record = post.get("record", {})
                text = record.get("text", "")
                matches = matching_keywords(text, keywords)
                if not matches:
                    continue

                uri = post.get("uri", "")
                if not uri:
                    continue

                if uri not in collected:
                    author = post.get("author", {})
                    collected[uri] = {
                        "post_id": uri,
                        "keyword_busca": keyword,
                        "keywords_encontradas": set(matches),
                        "texto": text,
                        "data_publicacao": record.get("createdAt", ""),
                        "username": author.get("handle", ""),
                        "nome_exibicao": author.get("displayName", ""),
                        "url": post_url(post),
                        "likes": post.get("likeCount", 0),
                        "reposts": post.get("repostCount", 0),
                        "replies": post.get("replyCount", 0),
                        "quotes": post.get("quoteCount", 0),
                        "coletado_em_utc": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    collected[uri]["keywords_encontradas"].update(matches)

            cursor = payload.get("cursor")
            if not cursor or len(posts) < limit_per_keyword:
                break
            # Public searches may reject cursor-based pagination with HTTP 403.
            if page_number == 0 and max_pages > 1:
                print("  Paginação ignorada: a busca pública pode bloquear cursores (HTTP 403).")
                break
            if page_number + 1 < max_pages:
                time.sleep(delay)

        if position < len(keywords):
            time.sleep(delay)

    return collected


def collect_comments(
    posts: OrderedDict[str, dict[str, Any]],
    keywords: list[str],
    language: str | None,
    depth: int,
    max_posts: int,
    max_comments_per_post: int,
    delay: float,
    timeout: int,
) -> OrderedDict[str, dict[str, Any]]:
    """Collect replies to matched posts and remove duplicates."""
    comments: OrderedDict[str, dict[str, Any]] = OrderedDict()
    targets = [post for post in posts.values() if int(post.get("replies", 0) or 0) > 0]
    if max_posts > 0:
        targets = targets[:max_posts]

    print(f"Buscando comentários de {len(targets)} posts com respostas.")

    for position, source in enumerate(targets, start=1):
        source_uri = source["post_id"]
        print(f"  [{position}/{len(targets)}] Comentários de: {source_uri}")

        try:
            payload = fetch_thread(source_uri, depth, timeout)
        except HTTPError as error:
            print(f"    Ignorado: API respondeu HTTP {error.code}.")
            continue
        except (URLError, TimeoutError) as error:
            print(f"    Ignorado: não foi possível conectar à API ({error}).")
            continue

        thread = payload.get("thread", {})
        replies = thread.get("replies", []) if isinstance(thread, dict) else []
        stack: list[tuple[dict[str, Any], int]] = [
            (reply, 1) for reply in reversed(replies) if isinstance(reply, dict)
        ]
        examined = 0

        while stack and examined < max_comments_per_post:
            node, level = stack.pop()
            child_replies = node.get("replies", [])
            stack.extend(
                (reply, level + 1)
                for reply in reversed(child_replies)
                if isinstance(reply, dict)
            )

            comment = node.get("post")
            if not isinstance(comment, dict):
                continue

            examined += 1
            if not matches_language(comment, language):
                continue

            uri = comment.get("uri", "")
            if not uri or uri == source_uri:
                continue

            record = comment.get("record", {})
            text = record.get("text", "")
            reply_data = record.get("reply", {})
            root_uri = reply_data.get("root", {}).get("uri", source_uri)
            parent_uri = reply_data.get("parent", {}).get("uri", source_uri)
            matches = matching_keywords(text, keywords)

            if uri not in comments:
                author = comment.get("author", {})
                comments[uri] = {
                    "comentario_id": uri,
                    "posts_encontrados": {source_uri},
                    "post_raiz_id": root_uri,
                    "post_pai_id": parent_uri,
                    "nivel_resposta": level,
                    "keywords_dos_posts": set(source["keywords_encontradas"]),
                    "keywords_no_comentario": set(matches),
                    "texto": text,
                    "data_publicacao": record.get("createdAt", ""),
                    "username": author.get("handle", ""),
                    "nome_exibicao": author.get("displayName", ""),
                    "url": post_url(comment),
                    "likes": comment.get("likeCount", 0),
                    "reposts": comment.get("repostCount", 0),
                    "replies": comment.get("replyCount", 0),
                    "quotes": comment.get("quoteCount", 0),
                    "coletado_em_utc": datetime.now(timezone.utc).isoformat(),
                }
            else:
                comments[uri]["posts_encontrados"].add(source_uri)
                comments[uri]["keywords_dos_posts"].update(
                    source["keywords_encontradas"]
                )
                comments[uri]["keywords_no_comentario"].update(matches)
                comments[uri]["nivel_resposta"] = min(
                    comments[uri]["nivel_resposta"], level
                )

        if position < len(targets):
            time.sleep(delay)

    return comments


def hash_identifier(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_state(path: Path) -> tuple[set[str], set[str]]:
    """Load fingerprints for records already saved."""
    if not path.exists():
        return set(), set()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Não foi possível ler o estado em {path}: {error}") from error

    return set(payload.get("posts", [])), set(payload.get("comentarios", []))


def save_state(path: Path, post_ids: set[str], comment_ids: set[str]) -> None:
    """Save state atomically so it survives restarts."""
    payload = {
        "posts": sorted(post_ids),
        "comentarios": sorted(comment_ids),
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def fingerprint_from_csv(value: str) -> str:
    """Accept raw IDs and hashes from anonymized collections."""
    normalized = value.strip().lower()
    if len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized):
        return normalized
    return hash_identifier(value)


def include_existing_csv_ids(path: Path, field: str, target: set[str]) -> None:
    """Add existing CSV records to the monitor state."""
    if not path.exists() or path.stat().st_size == 0:
        return

    with path.open("r", newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            value = row.get(field, "").strip()
            if value:
                target.add(fingerprint_from_csv(value))


def only_new_records(
    records: OrderedDict[str, dict[str, Any]],
    seen: set[str],
) -> OrderedDict[str, dict[str, Any]]:
    return OrderedDict(
        (identifier, record)
        for identifier, record in records.items()
        if hash_identifier(identifier) not in seen
    )


def append_posts_csv(
    posts: OrderedDict[str, dict[str, Any]], output: Path, omit_identifiers: bool
) -> None:
    fields = [
        "post_id",
        "keyword_busca",
        "keywords_encontradas",
        "texto",
        "data_publicacao",
        "username",
        "nome_exibicao",
        "url",
        "likes",
        "reposts",
        "replies",
        "quotes",
        "coletado_em_utc",
    ]

    write_header = not output.exists() or output.stat().st_size == 0
    with output.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if write_header:
            writer.writeheader()
        for post in posts.values():
            row = dict(post)
            row["keywords_encontradas"] = ", ".join(sorted(row["keywords_encontradas"]))
            if omit_identifiers:
                row["post_id"] = hashlib.sha256(
                    row["post_id"].encode("utf-8")
                ).hexdigest()
                row["username"] = ""
                row["nome_exibicao"] = ""
                row["url"] = ""
            writer.writerow(row)


def append_comments_csv(
    comments: OrderedDict[str, dict[str, Any]],
    output: Path,
    omit_identifiers: bool,
) -> None:
    fields = [
        "comentario_id",
        "posts_encontrados",
        "post_raiz_id",
        "post_pai_id",
        "nivel_resposta",
        "keywords_dos_posts",
        "keywords_no_comentario",
        "texto",
        "data_publicacao",
        "username",
        "nome_exibicao",
        "url",
        "likes",
        "reposts",
        "replies",
        "quotes",
        "coletado_em_utc",
    ]

    write_header = not output.exists() or output.stat().st_size == 0
    with output.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if write_header:
            writer.writeheader()

        for comment in comments.values():
            row = dict(comment)
            source_ids = sorted(row["posts_encontrados"])
            row["keywords_dos_posts"] = ", ".join(
                sorted(row["keywords_dos_posts"])
            )
            row["keywords_no_comentario"] = ", ".join(
                sorted(row["keywords_no_comentario"])
            )

            if omit_identifiers:
                row["comentario_id"] = hash_identifier(row["comentario_id"])
                row["posts_encontrados"] = "; ".join(
                    hash_identifier(value) for value in source_ids
                )
                row["post_raiz_id"] = hash_identifier(row["post_raiz_id"])
                row["post_pai_id"] = hash_identifier(row["post_pai_id"])
                row["username"] = ""
                row["nome_exibicao"] = ""
                row["url"] = ""
            else:
                row["posts_encontrados"] = "; ".join(source_ids)

            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coleta posts públicos do Bluesky por keywords e salva em CSV."
    )
    parser.add_argument(
        "--keywords-file",
        type=Path,
        default=DEFAULT_KEYWORDS_FILE,
        help="Arquivo UTF-8 com uma keyword por linha.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help="Arquivo CSV de saída.",
    )
    parser.add_argument(
        "--comments-output",
        type=Path,
        default=DEFAULT_COMMENTS_FILE,
        help="Arquivo CSV que receberá os comentários.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Arquivo usado para lembrar os registros já coletados.",
    )
    parser.add_argument(
        "--limit-per-keyword",
        type=int,
        default=25,
        choices=range(1, 101),
        metavar="1-100",
        help="Quantidade por página para cada keyword (padrão: 25).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        choices=range(1, 11),
        metavar="1-10",
        help="Máximo de páginas por keyword (padrão: 1).",
    )
    parser.add_argument(
        "--language",
        default="pt",
        help="Idioma ISO 639-1; use vazio para não filtrar (padrão: pt).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Pausa em segundos entre consultas (padrão: 0.5).",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=3.0,
        help="Intervalo entre ciclos de monitoramento (padrão: 3 minutos).",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Executa somente um ciclo e encerra.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Tempo máximo de espera da API em segundos (padrão: 30).",
    )
    parser.add_argument(
        "--comment-depth",
        type=int,
        default=10,
        choices=range(1, 11),
        metavar="1-10",
        help="Níveis de respostas encadeadas a coletar (padrão: 10).",
    )
    parser.add_argument(
        "--max-posts-for-comments",
        type=int,
        default=100,
        choices=range(0, 10001),
        metavar="0-10000",
        help="Máximo de posts cujos comentários serão consultados; 0 busca todos (padrão: 100).",
    )
    parser.add_argument(
        "--max-comments-per-post",
        type=int,
        default=100,
        choices=range(1, 1001),
        metavar="1-1000",
        help="Máximo de comentários examinados por post (padrão: 100).",
    )
    parser.add_argument(
        "--skip-comments",
        action="store_true",
        help="Coleta somente os posts, sem consultar os comentários.",
    )
    parser.add_argument(
        "--omit-identifiers",
        action="store_true",
        help="Remove username, nome, link e substitui o ID por um hash no CSV.",
    )
    return parser


def run_cycle(
    args: argparse.Namespace,
    keywords: list[str],
    language: str | None,
    seen_posts: set[str],
    seen_comments: set[str],
) -> tuple[int, int]:
    print(f"Iniciando ciclo com {len(keywords)} keywords.")
    posts = collect(
        keywords=keywords,
        limit_per_keyword=args.limit_per_keyword,
        max_pages=args.max_pages,
        language=language,
        delay=args.delay,
        timeout=args.timeout,
    )

    new_posts = only_new_records(posts, seen_posts)
    append_posts_csv(new_posts, args.output, args.omit_identifiers)
    seen_posts.update(hash_identifier(identifier) for identifier in new_posts)
    print(f"Novos posts: {len(new_posts)} de {len(posts)} encontrados.")

    new_comments: OrderedDict[str, dict[str, Any]] = OrderedDict()
    if not args.skip_comments:
        comments = collect_comments(
            posts=posts,
            keywords=keywords,
            language=language,
            depth=args.comment_depth,
            max_posts=args.max_posts_for_comments,
            max_comments_per_post=args.max_comments_per_post,
            delay=args.delay,
            timeout=args.timeout,
        )
        new_comments = only_new_records(comments, seen_comments)
        append_comments_csv(
            new_comments,
            args.comments_output,
            args.omit_identifiers,
        )
        seen_comments.update(
            hash_identifier(identifier) for identifier in new_comments
        )
        print(
            f"Novos comentários: {len(new_comments)} de "
            f"{len(comments)} encontrados."
        )

    save_state(args.state_file, seen_posts, seen_comments)
    return len(new_posts), len(new_comments)


def main() -> None:
    args = build_parser().parse_args()
    if args.interval_minutes <= 0:
        raise ValueError("--interval-minutes deve ser maior que zero.")

    keywords = read_keywords(args.keywords_file)
    language = args.language.strip() or None
    seen_posts, seen_comments = load_state(args.state_file)

    # Seed state from existing files to avoid duplicate records.
    include_existing_csv_ids(args.output, "post_id", seen_posts)
    include_existing_csv_ids(
        args.comments_output,
        "comentario_id",
        seen_comments,
    )
    save_state(args.state_file, seen_posts, seen_comments)

    print(
        f"Monitor iniciado. Intervalo: {args.interval_minutes:g} minutos. "
        "Pressione Ctrl+C para encerrar."
    )

    try:
        while True:
            cycle_started = time.monotonic()
            run_cycle(
                args,
                keywords,
                language,
                seen_posts,
                seen_comments,
            )

            if args.run_once:
                break

            elapsed = time.monotonic() - cycle_started
            wait_seconds = max(0.0, args.interval_minutes * 60 - elapsed)
            print(
                f"Ciclo concluído. Próxima busca em "
                f"{wait_seconds / 60:.1f} minutos."
            )
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        print("\nMonitor encerrado. O estado foi preservado.")


if __name__ == "__main__":
    main()
