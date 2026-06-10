import hashlib
import asyncio
import json
import threading
import time

import zstandard as zstd
from asgiref.sync import sync_to_async
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from .models import QueryExecutionLog, ReportDatasetCache, ReportDatasetCacheLock
from .query_execution import async_execute_query, execute_query, sql_preview


class ReportCacheError(Exception):
    pass


CACHE_HIT_CLEANUP_INTERVAL = 10
_cache_hit_counter = 0
_cache_hit_counter_lock = threading.Lock()


def report_dataset_cache_key(report, dataset_name="primary"):
    identity = {
        "report_id": report.id,
        "dataset_name": dataset_name,
        "database_connection_id": report.database_connection_id,
        "sql": report.primary_sql,
        "max_rows": report.organization.max_rows,
        "max_raw_bytes": report.organization.max_raw_bytes,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_report_dataset(report, dataset_name="primary", user=None):
    cache_key = report_dataset_cache_key(report, dataset_name)
    cache = get_fresh_report_dataset_cache(cache_key)
    if cache:
        payload = decompress_payload(cache.compressed_payload)
        log_cache_hit(cache, user=user)
        return payload, True

    lock = get_or_create_cache_lock(cache_key)
    with transaction.atomic():
        ReportDatasetCacheLock.objects.select_for_update().get(id=lock.id)
        cache = get_fresh_report_dataset_cache(cache_key)
        if cache:
            payload = decompress_payload(cache.compressed_payload)
            log_cache_hit(cache, user=user)
            return payload, True

        error = None
        payload = None
        try:
            result = execute_query(report.database_connection, report.primary_sql, user=user)
            payload = {
                "columns": result.columns,
                "rows": result.rows,
                "row_count": result.row_count,
                "raw_bytes": result.raw_bytes,
            }
            store_report_dataset_cache(report, payload, cache_key, dataset_name)
        except Exception as exc:
            error = exc

    if error:
        raise error
    return payload, False


async def async_get_report_dataset(report, dataset_name="primary", user=None):
    cache_key = report_dataset_cache_key(report, dataset_name)
    cache = await sync_to_async(get_fresh_report_dataset_cache, thread_sensitive=True)(
        cache_key
    )
    if cache:
        payload = decompress_payload(cache.compressed_payload)
        await async_log_cache_hit(cache, user=user)
        return payload, True

    await sync_to_async(get_or_create_cache_lock, thread_sensitive=True)(cache_key)
    error = None
    payload = None
    cache = await sync_to_async(get_fresh_report_dataset_cache, thread_sensitive=True)(
        cache_key
    )
    if cache:
        payload = decompress_payload(cache.compressed_payload)
        await async_log_cache_hit(cache, user=user)
        return payload, True

    try:
        result = await async_execute_query(
            report.database_connection,
            report.primary_sql,
            user=user,
        )
        payload = {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "raw_bytes": result.raw_bytes,
        }
        await sync_to_async(store_report_dataset_cache, thread_sensitive=True)(
            report,
            payload,
            cache_key,
            dataset_name,
        )
    except Exception as exc:
        error = exc

    if error:
        raise error
    return payload, False


def get_fresh_report_dataset_cache(cache_key):
    return (
        ReportDatasetCache.objects.filter(
            cache_key=cache_key,
            expires_at__gt=timezone.now(),
        )
        .select_related("database_connection", "organization")
        .first()
    )


def get_or_create_cache_lock(cache_key):
    try:
        lock, _created = ReportDatasetCacheLock.objects.get_or_create(cache_key=cache_key)
    except IntegrityError:
        lock = ReportDatasetCacheLock.objects.get(cache_key=cache_key)
    return lock


def store_report_dataset_cache(report, payload, cache_key, dataset_name):
    organization = report.organization
    compressed_payload = compress_payload(payload)
    compressed_bytes = len(compressed_payload)
    if compressed_bytes > organization.max_compressed_bytes:
        raise ReportCacheError(
            f"Cached query result is {compressed_bytes} compressed bytes, above the allowed {organization.max_compressed_bytes} bytes."
        )
    expires_at = timezone.now() + timezone.timedelta(seconds=organization.cache_ttl_seconds)
    ReportDatasetCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            "organization": organization,
            "report": report,
            "database_connection": report.database_connection,
            "dataset_name": dataset_name,
            "sql_preview": sql_preview(report.primary_sql),
            "compressed_payload": compressed_payload,
            "raw_bytes": payload["raw_bytes"],
            "compressed_bytes": compressed_bytes,
            "row_count": payload["row_count"],
            "expires_at": expires_at,
        },
    )


def compress_payload(payload):
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    return zstd.ZstdCompressor().compress(raw)


def decompress_payload(compressed_payload):
    raw = zstd.ZstdDecompressor().decompress(bytes(compressed_payload))
    return json.loads(raw.decode("utf-8"))


def log_cache_hit(cache, user=None):
    started = time.monotonic()
    QueryExecutionLog.objects.create(
        organization=cache.organization,
        database_connection=cache.database_connection,
        user=user if user and user.is_authenticated else None,
        sql_preview=cache.sql_preview,
        succeeded=True,
        row_count=cache.row_count,
        raw_bytes=cache.raw_bytes,
        duration_ms=int((time.monotonic() - started) * 1000),
        cache_status=QueryExecutionLog.CacheStatus.HIT,
    )
    maybe_schedule_expired_cache_cleanup()


async def async_log_cache_hit(cache, user=None):
    started = time.monotonic()
    await sync_to_async(QueryExecutionLog.objects.create, thread_sensitive=True)(
        organization=cache.organization,
        database_connection=cache.database_connection,
        user=user if user and user.is_authenticated else None,
        sql_preview=cache.sql_preview,
        succeeded=True,
        row_count=cache.row_count,
        raw_bytes=cache.raw_bytes,
        duration_ms=int((time.monotonic() - started) * 1000),
        cache_status=QueryExecutionLog.CacheStatus.HIT,
    )
    await async_maybe_schedule_expired_cache_cleanup()


def maybe_schedule_expired_cache_cleanup():
    global _cache_hit_counter
    with _cache_hit_counter_lock:
        _cache_hit_counter += 1
        should_cleanup = _cache_hit_counter % CACHE_HIT_CLEANUP_INTERVAL == 0
    if should_cleanup:
        schedule_expired_cache_cleanup()


async def async_maybe_schedule_expired_cache_cleanup():
    global _cache_hit_counter
    with _cache_hit_counter_lock:
        _cache_hit_counter += 1
        should_cleanup = _cache_hit_counter % CACHE_HIT_CLEANUP_INTERVAL == 0
    if should_cleanup:
        asyncio.create_task(async_run_expired_cache_cleanup())


def schedule_expired_cache_cleanup():
    thread = threading.Thread(
        target=run_expired_cache_cleanup,
        name="report-cache-cleanup",
        daemon=True,
    )
    thread.start()


def run_expired_cache_cleanup():
    close_old_connections()
    try:
        cleanup_expired_report_dataset_caches()
    except Exception:
        pass
    finally:
        close_old_connections()


def cleanup_expired_report_dataset_caches():
    deleted = ReportDatasetCache.objects.filter(expires_at__lte=timezone.now()).delete()
    ReportDatasetCacheLock.objects.exclude(
        cache_key__in=ReportDatasetCache.objects.values("cache_key")
    ).delete()
    return deleted


async def async_run_expired_cache_cleanup():
    try:
        await async_cleanup_expired_report_dataset_caches()
    except Exception:
        pass


async def async_cleanup_expired_report_dataset_caches():
    deleted = await ReportDatasetCache.objects.filter(
        expires_at__lte=timezone.now()
    ).adelete()
    await ReportDatasetCacheLock.objects.exclude(
        cache_key__in=ReportDatasetCache.objects.values("cache_key")
    ).adelete()
    return deleted
