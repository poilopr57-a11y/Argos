[app]

# Application metadata
title = ARGOS CLI
package.name = argos_cli
package.domain = org.iliyaqdrwalqu.argos
version = 2.1.3

# Source — CLI entry, no Kivy
source.dir = .
source.main = main_cli.py
source.include_exts = py,json,txt,md,env,yaml,yml
source.include_patterns = src/*,plugins/*,config/*

# Requirements — minimal, no Kivy/Cython
requirements = python3,requests,python-dotenv

# Orientation
orientation = portrait
fullscreen = 0

# Android permissions
android.permissions = INTERNET,ACCESS_NETWORK_STATE,FOREGROUND_SERVICE

# Android API / NDK / SDK
android.api = 33
android.minapi = 24
android.ndk = 27b
android.archs = arm64-v8a

# Build settings — service_only bootstrap avoids SDL2/Kivy native compilation
android.accept_sdk_license = True
p4a.branch = develop
p4a.bootstrap = service_only

# Icons
icon.filename = %(source.dir)s/assets/argos_icon_512.png

[buildozer]
log_level = 2
warn_on_root = 0
