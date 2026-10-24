package com.scoopz.app.data

import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Index
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow

enum class Status { QUEUED, DOWNLOADING, DONE, FAILED }
enum class CaptionState { NONE, RUNNING, DONE, FAILED }

@Entity(tableName = "videos", indices = [Index("videoId")])
data class VideoEntity(
    @PrimaryKey val sourceUrl: String,
    val videoId: String? = null,
    val author: String? = null,
    val title: String? = null,
    val status: Status = Status.QUEUED,
    val mediaUri: String? = null,
    val caption: String? = null,
    val hashtags: String? = null,
    val captionState: CaptionState = CaptionState.NONE,
    val error: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

@Dao
interface VideoDao {
    @Query("SELECT * FROM videos ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<VideoEntity>>

    @Query("SELECT * FROM videos WHERE sourceUrl = :url")
    suspend fun get(url: String): VideoEntity?

    @Query("SELECT * FROM videos WHERE videoId = :videoId AND status = 'DONE' LIMIT 1")
    suspend fun findDone(videoId: String): VideoEntity?

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(v: VideoEntity): Long

    @Query("UPDATE videos SET status = :status, error = :error WHERE sourceUrl = :url")
    suspend fun setStatus(url: String, status: Status, error: String? = null)

    @Query("UPDATE videos SET videoId = :videoId, author = :author, title = :title WHERE sourceUrl = :url")
    suspend fun setMeta(url: String, videoId: String, author: String, title: String)

    @Query("UPDATE videos SET status = 'DONE', mediaUri = :uri, error = NULL WHERE sourceUrl = :url")
    suspend fun setDone(url: String, uri: String)

    @Query("UPDATE videos SET captionState = :state, error = :error WHERE sourceUrl = :url")
    suspend fun setCaptionState(url: String, state: CaptionState, error: String? = null)

    @Query(
        "UPDATE videos SET caption = :caption, hashtags = :hashtags, " +
            "captionState = 'DONE', error = NULL WHERE sourceUrl = :url"
    )
    suspend fun setCaption(url: String, caption: String, hashtags: String)

    @Query("DELETE FROM videos WHERE sourceUrl = :url")
    suspend fun delete(url: String)
}

@Database(entities = [VideoEntity::class], version = 1, exportSchema = false)
abstract class ScoopzDb : RoomDatabase() {
    abstract fun dao(): VideoDao
}
