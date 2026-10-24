package com.scoopz.app

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.decode.VideoFrameDecoder
import coil.disk.DiskCache
import coil.memory.MemoryCache
import com.scoopz.app.caption.NvidiaCaptioner
import com.scoopz.app.caption.RateLimiter
import com.scoopz.app.data.ScoopzDb
import com.scoopz.app.error.ErrorAi
import com.scoopz.app.data.Settings
import com.scoopz.app.net.TikwmClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit
import androidx.room.Room

/** Hand-rolled service locator: one DB, one HTTP client, shared by UI and workers. */
class Graph(app: Application) {
    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val db: ScoopzDb = Room.databaseBuilder(app, ScoopzDb::class.java, "scoopz.db").build()
    val settings = Settings(app)
    val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()
    val tikwm = TikwmClient(http)
    val limiter = RateLimiter { settings.requestsPerMinute }
    val captioner = NvidiaCaptioner(http, settings, limiter)
    val errorAi = ErrorAi(http, settings, limiter)
}

class ScoopzApp : Application(), ImageLoaderFactory {
    override fun onCreate() {
        super.onCreate()
        graph = Graph(this)
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Downloads & captions", NotificationManager.IMPORTANCE_LOW)
        )
    }

    /** Small, bounded caches keep the library grid from ballooning RAM or disk. */
    override fun newImageLoader(): ImageLoader = ImageLoader.Builder(this)
        .components { add(VideoFrameDecoder.Factory()) }
        .memoryCache { MemoryCache.Builder(this).maxSizePercent(0.12).build() }
        .diskCache {
            DiskCache.Builder().directory(cacheDir.resolve("thumbs")).maxSizeBytes(64L * 1024 * 1024).build()
        }
        .crossfade(true)
        .build()

    companion object {
        const val CHANNEL_ID = "scoopz_work"
        lateinit var graph: Graph
            private set
    }
}
