import sys
import os
import json

def main():
    try:
        from server.settings_gui import show_settings_window
    except Exception as e:
        print('Failed to import settings GUI:', e)
        return 2

    try:
        show_settings_window()
    except Exception as e:
        print('Settings GUI error:', e)
        return 1

    return 0

if __name__ == '__main__':
    sys.exit(main())
import sys

def main():
    try:
        from settings_gui import show_settings_window
    except Exception as e:
        print('Failed to import settings_gui:', e)
        return 2

    try:
        show_settings_window()
    except Exception as e:
        print('Settings GUI failed:', e)
        return 1

    return 0

if __name__ == '__main__':
    sys.exit(main())
