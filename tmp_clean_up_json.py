import json
import os
from urllib.parse import urlparse, parse_qs
def get_correct_url_part(url):
    """
    Extrahiert die YouTube-Video-ID sicher aus verschiedenen URL-Formaten.
    Unterstützt standard (youtube.com) und gekürzte (youtu.be) Links.
    """
    parsed_url = urlparse(url)
    
    if "youtube.com" in parsed_url.netloc:
        query_params = parse_qs(parsed_url.query)
        video_id = query_params.get("v")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id[0]}"
            
    elif "youtu.be" in parsed_url.netloc:
        video_id = parsed_url.path.strip("/")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
            
    clean_path = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    return clean_path if clean_path != "://" else url

def clean_up_json_file(file_path: str):
    """
    Analysiert die Änderungen, gibt sie in der Konsole aus und überschreibt
    die Datei erst nach Bestätigung des Nutzers.
    """
    full_path = os.path.join(os.path.dirname(__file__), file_path)
    
    if not os.path.exists(full_path):
        print(f"Fehler: Datei '{full_path}' nicht gefunden.")
        return
        
    with open(full_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    cleaned_data = {}
    changes_logged = False
    
    print("--- Geplante Änderungen werden analysiert ---\n")
    
    for original_url, title in data.items():
        clean_url = get_correct_url_part(original_url)
        
        if original_url != clean_url:
            print(f"[ÄNDERUNG] URL wird gekürzt:")
            print(f"  Alt: {original_url}")
            print(f"  Neu: {clean_url}\n")
            changes_logged = True
            
        if clean_url in cleaned_data and cleaned_data[clean_url] != title:
            print(f"[WARNUNG] Duplikat-Konflikt! Der alte Titel wird überschrieben:")
            print(f"  URL: {clean_url}")
            print(f"  Bisheriger Titel: '{cleaned_data[clean_url]}'")
            print(f"  Neuer Titel:      '{title}'\n")
            changes_logged = True
            
        cleaned_data[clean_url] = title
    if not changes_logged:
        print("Alles sauber! Es sind keine Änderungen an der JSON-Datei notwendig.")
        return
    proceed = input("Möchten Sie diese Änderungen anwenden? (y/n): ").strip().lower()
    
    if proceed == 'y':
        with open(full_path, 'w', encoding='utf-8') as f:
            json.dump(cleaned_data, f, indent=2, ensure_ascii=False)
        print(f"\nErfolgreich! Die Datei '{file_path}' wurde aktualisiert.")
    else:
        print("\nAbgebrochen. Es wurden keine Änderungen an der Datei vorgenommen.")
        
if __name__ == "__main__":
    clean_up_json_file(r"server\name_databank.json")
