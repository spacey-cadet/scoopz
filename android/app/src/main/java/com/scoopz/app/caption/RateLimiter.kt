package com.scoopz.app.caption

import android.os.SystemClock
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Spaces requests evenly so we stay under the free tier's requests-per-minute cap. */
class RateLimiter(private val rpmProvider: () -> Int) {
    private val mutex = Mutex()
    private var nextAllowedAt = 0L

    suspend fun acquire() {
        mutex.withLock {
            val interval = 60_000L / rpmProvider().coerceAtLeast(1)
            val now = SystemClock.elapsedRealtime()
            val start = maxOf(now, nextAllowedAt)
            if (start > now) delay(start - now)
            nextAllowedAt = start + interval
        }
    }

    /** Push the window out after a 429 so queued requests back off too. */
    fun penalize(ms: Long) {
        nextAllowedAt = maxOf(nextAllowedAt, SystemClock.elapsedRealtime() + ms)
    }
}
