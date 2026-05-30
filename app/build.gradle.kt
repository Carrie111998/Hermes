// app/build.gradle.kts
android {
    // ... altre configurazioni

    buildFeatures {
        // Abilita la generazione della classe BuildConfig
        buildConfig = true 
    }

    defaultConfig {
        // ...
        // Definisce il campo API_KEY (recuperato da local.properties o env)
        val apiKey = project.findProperty("API_KEY") as? String ?: "VALORE_DI_BACKUP"
        buildConfigField("String", "API_KEY", "\"$apiKey\"")
    }
}