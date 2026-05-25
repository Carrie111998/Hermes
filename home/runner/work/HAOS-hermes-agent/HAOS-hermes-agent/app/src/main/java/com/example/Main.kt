// app/build.gradle.kts
android {
    // ... altre configurazioni esistenti ...

    defaultConfig {
        // ...
        // Definisce la costante per la chiave API leggendola in sicurezza
        val geminiApiKey = project.findProperty("GEMINI_API_KEY") ?: System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // Abilita correttamente la generazione della classe BuildConfig
        buildConfig = true
    }
}