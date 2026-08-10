import json
import sys
import os

def main():
    if len(sys.argv) < 2:
        print('Usage: python -m server.select_cli <payload.json>')
        return 2

    path = sys.argv[1]
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print('Failed to read payload:', e)
        return 1

    signal = data.get('signal')
    urls = data.get('urls', [])

    try:
        # import and show the selection UI
        from server.select import show_selection_window
        show_selection_window(signal, urls)
    except Exception as e:
        print('Selector failed:', e)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

if __name__ == '__main__':
    sys.exit(main())
