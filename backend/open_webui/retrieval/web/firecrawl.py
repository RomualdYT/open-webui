from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
import requests
from langchain_core.documents import Document

if TYPE_CHECKING:
    from open_webui.retrieval.web.main import SearchResult

log = logging.getLogger(__name__)

DEFAULT_FIRECRAWL_API_BASE_URL = 'https://api.firecrawl.dev'
DEFAULT_FIRECRAWL_LOADER_ONLY_MAIN_CONTENT = True
DEFAULT_FIRECRAWL_LOADER_PARSE_PDF = True
DEFAULT_FIRECRAWL_LOADER_MULTI_URL_MODE = 'auto'
DEFAULT_FIRECRAWL_LOADER_PROXY_MODE = 'basic'
DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS = 3600000
FIRECRAWL_PROXY_MODES = {'basic', 'auto', 'enhanced'}
FIRECRAWL_MULTI_URL_MODES = {'auto', 'batch', 'single'}
FIRECRAWL_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
FIRECRAWL_MAX_RETRIES = 2


def _parse_positive_float(value: Any) -> float | None:
    if value in (None, ''):
        return None

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed > 0 else None


def get_firecrawl_request_timeout_seconds(timeout: Any) -> float | None:
    return _parse_positive_float(timeout)


def get_firecrawl_scrape_timeout_ms(timeout: Any) -> int | None:
    seconds = get_firecrawl_request_timeout_seconds(timeout)
    if seconds is None:
        return None

    # Firecrawl v2 accepts scrape timeouts between 1s and 300s.
    return min(300000, max(1000, int(seconds * 1000)))


def get_firecrawl_wait_timeout_seconds(timeout: Any, url_count: int = 1) -> int:
    seconds = get_firecrawl_request_timeout_seconds(timeout)
    if seconds is not None:
        return max(1, int(seconds))

    return max(30, url_count * 3)


def normalize_firecrawl_proxy_mode(proxy_mode: Any) -> str:
    if isinstance(proxy_mode, str):
        normalized = proxy_mode.strip().lower()
        if normalized in FIRECRAWL_PROXY_MODES:
            return normalized

    return DEFAULT_FIRECRAWL_LOADER_PROXY_MODE


def normalize_firecrawl_multi_url_mode(multi_url_mode: Any) -> str:
    if isinstance(multi_url_mode, str):
        normalized = multi_url_mode.strip().lower()
        if normalized in FIRECRAWL_MULTI_URL_MODES:
            return normalized

    return DEFAULT_FIRECRAWL_LOADER_MULTI_URL_MODE


def normalize_firecrawl_max_age_ms(max_age_ms: Any) -> int:
    if max_age_ms in (None, ''):
        return DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS

    try:
        max_age = int(max_age_ms)
    except (TypeError, ValueError):
        return DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS

    return max_age if max_age >= 0 else DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS


def _build_firecrawl_url(base_url: str | None, path: str) -> str:
    normalized_base_url = (base_url or DEFAULT_FIRECRAWL_API_BASE_URL).rstrip('/')
    normalized_path = path.lstrip('/')

    if normalized_base_url.endswith('/v2'):
        return f'{normalized_base_url}/{normalized_path}'

    return f'{normalized_base_url}/v2/{normalized_path}'


def _build_firecrawl_headers(api_key: str | None) -> dict[str, str]:
    return {
        'Authorization': f'Bearer {api_key or ""}',
        'Content-Type': 'application/json',
    }


def _get_retry_delay(headers: Any, attempt: int) -> float:
    retry_after = headers.get('Retry-After') if headers else None
    if retry_after:
        try:
            return min(10.0, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass

    return min(8.0, float(2**attempt))


def _request_firecrawl_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, headers=headers, json=json, timeout=timeout)

            if response.status_code in FIRECRAWL_RETRY_STATUS_CODES and attempt < FIRECRAWL_MAX_RETRIES:
                delay = _get_retry_delay(response.headers, attempt)
                log.warning(
                    'Firecrawl %s %s returned HTTP %s; retrying in %.1fs',
                    method,
                    url,
                    response.status_code,
                    delay,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
            if attempt >= FIRECRAWL_MAX_RETRIES:
                break

            delay = _get_retry_delay(None, attempt)
            log.warning('Firecrawl %s %s failed; retrying in %.1fs: %s', method, url, delay, e)
            time.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError(f'Firecrawl {method} {url} failed without a response')


async def _arequest_firecrawl_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
        try:
            async with session.request(method, url, headers=headers, json=json) as response:
                if response.status in FIRECRAWL_RETRY_STATUS_CODES and attempt < FIRECRAWL_MAX_RETRIES:
                    delay = _get_retry_delay(response.headers, attempt)
                    log.warning(
                        'Firecrawl %s %s returned HTTP %s; retrying in %.1fs',
                        method,
                        url,
                        response.status,
                        delay,
                    )
                    await response.read()
                    await asyncio.sleep(delay)
                    continue

                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientConnectionError, TimeoutError) as e:
            last_error = e
            if attempt >= FIRECRAWL_MAX_RETRIES:
                break

            delay = _get_retry_delay(None, attempt)
            log.warning('Firecrawl %s %s failed; retrying in %.1fs: %s', method, url, delay, e)
            await asyncio.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError(f'Firecrawl {method} {url} failed without a response')


def build_firecrawl_scrape_payload(
    *,
    verify_ssl: bool,
    timeout: Any,
    only_main_content: bool = DEFAULT_FIRECRAWL_LOADER_ONLY_MAIN_CONTENT,
    parse_pdf: bool = DEFAULT_FIRECRAWL_LOADER_PARSE_PDF,
    proxy_mode: str = DEFAULT_FIRECRAWL_LOADER_PROXY_MODE,
    max_age_ms: Any = DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS,
    formats: list[str] | None = None,
    remove_base64_images: bool = True,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'formats': formats or ['markdown'],
        'onlyMainContent': only_main_content,
        'parsers': ['pdf'] if parse_pdf else [],
        'proxy': normalize_firecrawl_proxy_mode(proxy_mode),
        'maxAge': normalize_firecrawl_max_age_ms(max_age_ms),
        'skipTlsVerification': not verify_ssl,
        'removeBase64Images': remove_base64_images,
    }

    scrape_timeout_ms = get_firecrawl_scrape_timeout_ms(timeout)
    if scrape_timeout_ms is not None:
        payload['timeout'] = scrape_timeout_ms

    if extra_payload:
        payload.update(extra_payload)

    return payload


def _get_firecrawl_result_url(result: dict[str, Any]) -> str | None:
    metadata = result.get('metadata') or {}
    return (
        result.get('url')
        or result.get('link')
        or metadata.get('url')
        or metadata.get('sourceURL')
        or metadata.get('source_url')
    )


def _filter_results(
    results: list[dict[str, Any]], filter_list: list[str] | None
) -> list[dict[str, Any]]:
    if not filter_list:
        return results

    from open_webui.retrieval.web.main import get_filtered_results

    candidates = [{'url': _get_firecrawl_result_url(result)} for result in results if _get_firecrawl_result_url(result)]
    allowed_urls = {item.get('url') for item in get_filtered_results(candidates, filter_list)}

    return [result for result in results if _get_firecrawl_result_url(result) in allowed_urls]


def _firecrawl_result_to_search_result(result: dict[str, Any]) -> SearchResult | None:
    url = _get_firecrawl_result_url(result)
    if not url:
        return None

    metadata = result.get('metadata') or {}

    from open_webui.retrieval.web.main import SearchResult

    return SearchResult(
        link=url,
        title=result.get('title') or metadata.get('title'),
        snippet=result.get('description') or result.get('snippet') or metadata.get('description'),
    )


def _firecrawl_result_to_document(result: dict[str, Any]) -> Document | None:
    content = result.get('markdown') or ''
    if not isinstance(content, str) or content.strip() == '':
        return None

    metadata = result.get('metadata') or {}
    source = _get_firecrawl_result_url(result) or ''
    document_metadata = {'source': source}

    if metadata.get('title'):
        document_metadata['title'] = metadata['title']
    if metadata.get('description'):
        document_metadata['description'] = metadata['description']

    return Document(page_content=content, metadata=document_metadata)


def _get_web_results(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    data = response_json.get('data') or {}
    if isinstance(data, list):
        return [result for result in data if isinstance(result, dict)]

    return [result for result in data.get('web', []) if isinstance(result, dict)]


def _poll_firecrawl_batch_scrape(
    *,
    base_url: str | None,
    api_key: str | None,
    job_id: str,
    wait_timeout: int,
    request_timeout: float | None,
) -> list[dict[str, Any]]:
    started_at = time.monotonic()
    status_url = _build_firecrawl_url(base_url, f'batch/scrape/{job_id}')
    headers = _build_firecrawl_headers(api_key)

    while True:
        data = _request_firecrawl_json('GET', status_url, headers=headers, timeout=request_timeout)
        status = data.get('status')

        if status == 'completed':
            return data.get('data') or []

        if status == 'failed':
            raise RuntimeError(f'Firecrawl batch scrape failed. result: {data}')

        if time.monotonic() - started_at >= wait_timeout:
            raise TimeoutError(f'Firecrawl batch scrape timed out. result: {data}')

        time.sleep(2)


async def _apoll_firecrawl_batch_scrape(
    *,
    session: aiohttp.ClientSession,
    base_url: str | None,
    api_key: str | None,
    job_id: str,
    wait_timeout: int,
) -> list[dict[str, Any]]:
    started_at = time.monotonic()
    status_url = _build_firecrawl_url(base_url, f'batch/scrape/{job_id}')
    headers = _build_firecrawl_headers(api_key)

    while True:
        data = await _arequest_firecrawl_json(session, 'GET', status_url, headers=headers)

        status = data.get('status')
        if status == 'completed':
            return data.get('data') or []

        if status == 'failed':
            raise RuntimeError(f'Firecrawl batch scrape failed. result: {data}')

        if time.monotonic() - started_at >= wait_timeout:
            raise TimeoutError(f'Firecrawl batch scrape timed out. result: {data}')

        await asyncio.sleep(2)


def _load_firecrawl_document(
    *,
    base_url: str | None,
    api_key: str | None,
    url: str,
    scrape_payload: dict[str, Any],
    request_timeout: float | None,
) -> Document | None:
    data = _request_firecrawl_json(
        'POST',
        _build_firecrawl_url(base_url, 'scrape'),
        headers=_build_firecrawl_headers(api_key),
        json={'url': url, **scrape_payload},
        timeout=request_timeout,
    )
    return _firecrawl_result_to_document(data.get('data') or {})


async def _aload_firecrawl_document(
    *,
    session: aiohttp.ClientSession,
    base_url: str | None,
    api_key: str | None,
    url: str,
    scrape_payload: dict[str, Any],
) -> Document | None:
    data = await _arequest_firecrawl_json(
        session,
        'POST',
        _build_firecrawl_url(base_url, 'scrape'),
        headers=_build_firecrawl_headers(api_key),
        json={'url': url, **scrape_payload},
    )

    return _firecrawl_result_to_document(data.get('data') or {})


def load_firecrawl_documents(
    base_url: str | None,
    api_key: str | None,
    urls: Sequence[str],
    *,
    verify_ssl: bool,
    timeout: Any,
    only_main_content: bool = DEFAULT_FIRECRAWL_LOADER_ONLY_MAIN_CONTENT,
    parse_pdf: bool = DEFAULT_FIRECRAWL_LOADER_PARSE_PDF,
    multi_url_mode: str = DEFAULT_FIRECRAWL_LOADER_MULTI_URL_MODE,
    proxy_mode: str = DEFAULT_FIRECRAWL_LOADER_PROXY_MODE,
    max_age_ms: Any = DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS,
    max_concurrency: int | None = None,
    params: dict[str, Any] | None = None,
) -> list[Document]:
    scrape_payload = build_firecrawl_scrape_payload(
        verify_ssl=verify_ssl,
        timeout=timeout,
        only_main_content=only_main_content,
        parse_pdf=parse_pdf,
        proxy_mode=proxy_mode,
        max_age_ms=max_age_ms,
        extra_payload=params,
    )
    request_timeout = (get_firecrawl_request_timeout_seconds(timeout) or 60) + 10
    multi_url_mode = normalize_firecrawl_multi_url_mode(multi_url_mode)

    if len(urls) == 1 or multi_url_mode == 'single':
        docs = [
            _load_firecrawl_document(
                base_url=base_url,
                api_key=api_key,
                url=url,
                scrape_payload=scrape_payload,
                request_timeout=request_timeout,
            )
            for url in urls
        ]
        return [doc for doc in docs if doc is not None]

    payload = {
        'urls': list(urls),
        **scrape_payload,
        'ignoreInvalidURLs': True,
    }
    if max_concurrency:
        payload['maxConcurrency'] = max(1, int(max_concurrency))

    result = _request_firecrawl_json(
        'POST',
        _build_firecrawl_url(base_url, 'batch/scrape'),
        headers=_build_firecrawl_headers(api_key),
        json=payload,
        timeout=request_timeout,
    )

    if result.get('status') == 'completed':
        results = result.get('data') or []
    else:
        job_id = result.get('id')
        if not job_id:
            raise RuntimeError(f'Firecrawl batch scrape did not return a job id. result: {result}')

        results = _poll_firecrawl_batch_scrape(
            base_url=base_url,
            api_key=api_key,
            job_id=job_id,
            wait_timeout=get_firecrawl_wait_timeout_seconds(timeout, len(urls)),
            request_timeout=request_timeout,
        )

    return [doc for doc in (_firecrawl_result_to_document(result) for result in results) if doc is not None]


async def aload_firecrawl_documents(
    base_url: str | None,
    api_key: str | None,
    urls: Sequence[str],
    *,
    verify_ssl: bool,
    timeout: Any,
    only_main_content: bool = DEFAULT_FIRECRAWL_LOADER_ONLY_MAIN_CONTENT,
    parse_pdf: bool = DEFAULT_FIRECRAWL_LOADER_PARSE_PDF,
    multi_url_mode: str = DEFAULT_FIRECRAWL_LOADER_MULTI_URL_MODE,
    proxy_mode: str = DEFAULT_FIRECRAWL_LOADER_PROXY_MODE,
    max_age_ms: Any = DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS,
    max_concurrency: int | None = None,
    params: dict[str, Any] | None = None,
) -> list[Document]:
    scrape_payload = build_firecrawl_scrape_payload(
        verify_ssl=verify_ssl,
        timeout=timeout,
        only_main_content=only_main_content,
        parse_pdf=parse_pdf,
        proxy_mode=proxy_mode,
        max_age_ms=max_age_ms,
        extra_payload=params,
    )
    request_timeout = (get_firecrawl_request_timeout_seconds(timeout) or 60) + 10
    timeout_config = aiohttp.ClientTimeout(total=request_timeout) if request_timeout else None
    multi_url_mode = normalize_firecrawl_multi_url_mode(multi_url_mode)

    async with aiohttp.ClientSession(timeout=timeout_config) as session:
        if len(urls) == 1 or multi_url_mode == 'single':
            semaphore = asyncio.Semaphore(max(1, int(max_concurrency))) if max_concurrency else None

            async def load_url(url: str) -> Document | None:
                if semaphore:
                    async with semaphore:
                        return await _aload_firecrawl_document(
                            session=session,
                            base_url=base_url,
                            api_key=api_key,
                            url=url,
                            scrape_payload=scrape_payload,
                        )

                return await _aload_firecrawl_document(
                    session=session,
                    base_url=base_url,
                    api_key=api_key,
                    url=url,
                    scrape_payload=scrape_payload,
                )

            docs = await asyncio.gather(*(load_url(url) for url in urls))
            return [doc for doc in docs if doc is not None]

        payload = {
            'urls': list(urls),
            **scrape_payload,
            'ignoreInvalidURLs': True,
        }
        if max_concurrency:
            payload['maxConcurrency'] = max(1, int(max_concurrency))

        result = await _arequest_firecrawl_json(
            session,
            'POST',
            _build_firecrawl_url(base_url, 'batch/scrape'),
            headers=_build_firecrawl_headers(api_key),
            json=payload,
        )

        if result.get('status') == 'completed':
            results = result.get('data') or []
        else:
            job_id = result.get('id')
            if not job_id:
                raise RuntimeError(f'Firecrawl batch scrape did not return a job id. result: {result}')

            results = await _apoll_firecrawl_batch_scrape(
                session=session,
                base_url=base_url,
                api_key=api_key,
                job_id=job_id,
                wait_timeout=get_firecrawl_wait_timeout_seconds(timeout, len(urls)),
            )

    return [doc for doc in (_firecrawl_result_to_document(result) for result in results) if doc is not None]


def search_firecrawl(
    firecrawl_url: str,
    firecrawl_api_key: str,
    query: str,
    count: int,
    filter_list: list[str] | None = None,
) -> list[SearchResult]:
    try:
        payload = {
            'query': query,
            'limit': count,
            'timeout': count * 3000,
            'ignoreInvalidURLs': True,
        }
        data = _request_firecrawl_json(
            'POST',
            _build_firecrawl_url(firecrawl_url, 'search'),
            headers=_build_firecrawl_headers(firecrawl_api_key),
            json=payload,
            timeout=count * 3 + 10,
        )

        results = _filter_results(_get_web_results(data), filter_list)[:count]
        search_results = [
            search_result
            for search_result in (_firecrawl_result_to_search_result(result) for result in results)
            if search_result is not None
        ]
        log.info(f'FireCrawl search results: {search_results}')
        return search_results
    except Exception as e:
        log.error(f'Error in FireCrawl search: {e}')
        return []


async def search_firecrawl_with_scrape(
    firecrawl_url: str,
    firecrawl_api_key: str,
    query: str,
    count: int,
    filter_list: list[str] | None = None,
    *,
    verify_ssl: bool = True,
    timeout: Any = None,
    only_main_content: bool = DEFAULT_FIRECRAWL_LOADER_ONLY_MAIN_CONTENT,
    parse_pdf: bool = DEFAULT_FIRECRAWL_LOADER_PARSE_PDF,
    proxy_mode: str = DEFAULT_FIRECRAWL_LOADER_PROXY_MODE,
    max_age_ms: Any = DEFAULT_FIRECRAWL_LOADER_MAX_AGE_MS,
) -> tuple[list[SearchResult], list[Document]]:
    scrape_options = build_firecrawl_scrape_payload(
        verify_ssl=verify_ssl,
        timeout=timeout,
        only_main_content=only_main_content,
        parse_pdf=parse_pdf,
        proxy_mode=proxy_mode,
        max_age_ms=max_age_ms,
    )
    payload = {
        'query': query,
        'limit': count,
        'timeout': count * 3000,
        'ignoreInvalidURLs': True,
        'scrapeOptions': scrape_options,
    }

    request_timeout = (get_firecrawl_request_timeout_seconds(timeout) or count * 3) + 10
    timeout_config = aiohttp.ClientTimeout(total=request_timeout)

    async with aiohttp.ClientSession(timeout=timeout_config) as session:
        data = await _arequest_firecrawl_json(
            session,
            'POST',
            _build_firecrawl_url(firecrawl_url, 'search'),
            headers=_build_firecrawl_headers(firecrawl_api_key),
            json=payload,
        )

    results = _filter_results(_get_web_results(data), filter_list)[:count]
    search_results = [
        search_result
        for search_result in (_firecrawl_result_to_search_result(result) for result in results)
        if search_result is not None
    ]
    docs = [
        document
        for document in (_firecrawl_result_to_document(result) for result in results)
        if document is not None
    ]

    return search_results, docs
