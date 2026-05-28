// app/build.gradle.kts
android {
    compileSdk = 34 // o la tua versione di SDK

    defaultConfig {
        // ... altre configurazioni

        // Recupera la chiave dalle variabili d'ambiente (es. in CI) o local.properties
        val geminiApiKey: String = System.getenv("GEMINI_API_KEY") ?: ""
        buildConfigField("String", "GEMINI_API_KEY", "\"$geminiApiKey\"")
    }

    buildFeatures {
        // Abilita la generazione di BuildConfig in Kotlin DSL
        buildConfig = true 
    }
}