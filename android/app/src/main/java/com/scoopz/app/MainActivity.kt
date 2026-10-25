package com.scoopz.app

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.scoopz.app.ui.LibraryScreen
import com.scoopz.app.ui.LibraryViewModel
import com.scoopz.app.ui.ModelFinderScreen
import com.scoopz.app.ui.SettingsScreen

class MainActivity : ComponentActivity() {
    private val vm: LibraryViewModel by viewModels()

    private val notifPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* optional */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33) notifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)

        setContent {
            val dark = isSystemInDarkTheme()
            val ctx = LocalContext.current
            val scheme = when {
                Build.VERSION.SDK_INT >= 31 -> if (dark) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
                dark -> darkColorScheme()
                else -> lightColorScheme()
            }
            MaterialTheme(colorScheme = scheme) {
                var screen by remember { mutableStateOf(Screen.Library) }
                when (screen) {
                    Screen.Library -> LibraryScreen(vm) { screen = Screen.Settings }
                    Screen.Settings -> SettingsScreen(
                        onBack = { screen = Screen.Library },
                        onFindModels = { screen = Screen.Finder },
                    )
                    Screen.Finder -> ModelFinderScreen(onBack = { screen = Screen.Settings })
                }
            }
        }
    }
}

private enum class Screen { Library, Settings, Finder }
