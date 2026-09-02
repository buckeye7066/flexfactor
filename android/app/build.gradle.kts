plugins {
    id("com.android.application")
}

android {
    namespace = "com.firer.console.flexfactor"
    compileSdk = 36

    buildFeatures {
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.firer.console.flexfactor"
        minSdk = 26
        targetSdk = 36
        versionCode = 30500
        versionName = "3.5.0"
        buildConfigField(
            "String",
            "FLEXFACTOR_CLOUD_URL",
            "\"https://flexfactor-cloud.vercel.app\"",
        )

        testInstrumentationRunner = "android.test.InstrumentationTestRunner"
    }

    val releaseKeystorePath = providers.environmentVariable("FLEXFACTOR_ANDROID_KEYSTORE").orNull
    signingConfigs {
        if (!releaseKeystorePath.isNullOrBlank()) {
            create("release") {
                storeFile = file(releaseKeystorePath)
                storePassword = providers.environmentVariable("FLEXFACTOR_ANDROID_STORE_PASSWORD").get()
                keyAlias = providers.environmentVariable("FLEXFACTOR_ANDROID_KEY_ALIAS").get()
                keyPassword = providers.environmentVariable("FLEXFACTOR_ANDROID_KEY_PASSWORD").get()
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            signingConfig = signingConfigs.findByName("release")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        create("play") {
            initWith(getByName("release"))
            matchingFallbacks += listOf("release")
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("com.goterl:lazysodium-android:5.2.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20250517")
}
