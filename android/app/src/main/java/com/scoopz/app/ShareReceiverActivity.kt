package com.scoopz.app

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import com.scoopz.app.util.LinkParser
import com.scoopz.app.work.Enqueue
import kotlinx.coroutines.launch

/** Invisible share-sheet target: parse, queue, toast, close. Everything else happens in the background. */
class ShareReceiverActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val links = LinkParser.extract(intent?.getStringExtra(Intent.EXTRA_TEXT))
        if (links.isEmpty()) {
            Toast.makeText(this, "No TikTok link found", Toast.LENGTH_SHORT).show()
        } else {
            val app = applicationContext
            ScoopzApp.graph.appScope.launch { links.forEach { Enqueue.link(app, it) } }
            Toast.makeText(this, "Queued ${links.size} link(s)", Toast.LENGTH_SHORT).show()
        }
        finish()
    }
}
