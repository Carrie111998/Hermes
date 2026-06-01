// app/build.gradle.kts
android {
    compileSdk = 34 // o la tua versione corrente

    defaultConfig {
        applicationId = "com.example.haos"
        // ... altre configurazioni ...

        // Definisce la costante leggendola da variabili d'ambiente o fallback locale
        val apiKey = System.getenv("API_KEY") ?: "CHIAVE_DI_TEST"
        buildConfigField("String", "API_KEY", "\"$apiKey\"")
    }

    buildFeatures {
        // CORREZIONE: Abilita correttamente la generazione della classe BuildConfig
        buildConfig = true
    }
}