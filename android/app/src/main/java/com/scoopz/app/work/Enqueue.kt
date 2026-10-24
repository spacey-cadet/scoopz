package com.scoopz.app.work

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.workDataOf
import com.scoopz.app.ScoopzApp
import com.scoopz.app.data.CaptionState
import com.scoopz.app.data.Status
import com.scoopz.app.data.VideoEntity
import java.util.concurrent.TimeUnit

object Enqueue {
    private val net = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    /** Adds a link to the queue (ignored if already present) and starts its download. */
    suspend fun link(context: Context, url: String): Boolean {
        val dao = ScoopzApp.graph.db.dao()
        val inserted = dao.insert(VideoEntity(sourceUrl = url)) != -1L
        if (!inserted) {
            val existing = dao.get(url)
            if (existing?.status != Status.FAILED) return false
            dao.setStatus(url, Status.QUEUED)
        }
        download(context, url, ExistingWorkPolicy.REPLACE)
        return inserted
    }

    suspend fun retry(context: Context, url: String) {
        ScoopzApp.graph.db.dao().setStatus(url, Status.QUEUED)
        download(context, url, ExistingWorkPolicy.REPLACE)
    }

    fun caption(context: Context, url: String) {
        val req = OneTimeWorkRequestBuilder<CaptionWorker>()
            .setInputData(workDataOf(CaptionWorker.KEY_URL to url))
            .setConstraints(net)
            .build()
        WorkManager.getInstance(context)
            .enqueueUniqueWork("cap:$url", ExistingWorkPolicy.KEEP, req)
    }

    suspend fun recaption(context: Context, url: String) {
        ScoopzApp.graph.db.dao().setCaptionState(url, CaptionState.NONE)
        WorkManager.getInstance(context).cancelUniqueWork("cap:$url")
        caption(context, url)
    }

    private fun download(context: Context, url: String, policy: ExistingWorkPolicy) {
        val req = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setInputData(workDataOf(DownloadWorker.KEY_URL to url))
            .setConstraints(net)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("dl:$url", policy, req)
    }
}
