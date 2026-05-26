// app/build.gradle.kts
android {
    // ... altre configurazioni ...

    buildFeatures {
        // Abilita la generazione della classe BuildConfig
        buildConfig = true
    }

    defaultConfig {
        // Definisci il campo per evitare errori a runtime (es. leggendo da local.properties o env)
        val geminiApiKey = project.findProperty("GEMINI_API_KEY") ?: System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }
}