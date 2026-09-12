[app]
# Self-explanatory. May be In English. If you change this, change it everywhere.
title = GCSE Revision
package.name = revisiontracker
package.domain = org.benli

source.dir = .
source.include_exts = py,png,jpg,kv,ttf,db
source.include_patterns = revision_core.py,main.py,buildozer.spec
version = 1.0.0

# Requirements (pip packages)
requirements = python3,kivy==2.3.1

# Android specifics
android.permissions = INTERNET,ACCESS_WIFI_STATE,ACCESS_NETWORK_STATE
android.api = 34
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = True
android.accept_sdk_license = True

# Orientation
orientation = portrait

[buildozer]
log_level = 2
warn_on_root = 1