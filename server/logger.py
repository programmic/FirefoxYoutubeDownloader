import enum

class Log(enum.Enum):
    TIMESTAMP = "timestamp"
    DOWNLOAD_DURATION = "download_duration"
    ENCODING_DURATION = "encoding_duration"
    FILENAME = "filename"
    URL = "url"
    SBCUT = "sbcut"
    ORIGINAL_SIZE = "original_size"
    FINAL_SIZE = "final_size"
    PERCENTAGE_REDUCTION = "percentage_reduction"
    


class Logger:
    def __init__(self):
        self.current_doc: dict = {}

    def __repr__(self):
        all_size_variables: bool = Log.ORIGINAL_SIZE in self.current_doc and Log.FINAL_SIZE in self.current_doc and Log.PERCENTAGE_REDUCTION in self.current_doc
        repres = (
            f"{'='*30}",
            self.current_doc[Log.FILENAME] if Log.FILENAME in self.current_doc else "Title not available",
            self.current_doc[Log.URL] if Log.URL in self.current_doc else "URL not available",
            f"{'='*30}",
            f"Timestamp: {self.current_doc[Log.TIMESTAMP] if Log.TIMESTAMP in self.current_doc else 'Timestamp not available'}",
            f"Download Duration: {self.current_doc[Log.DOWNLOAD_DURATION] if Log.DOWNLOAD_DURATION in self.current_doc else 'Download duration not available'}",
            f"Encoding Duration: {self.current_doc[Log.ENCODING_DURATION] if Log.ENCODING_DURATION in self.current_doc else 'Encoding duration not available'}",
            f"{self.current_doc[Log.ORIGINAL_SIZE]} >>> {self.current_doc[Log.FINAL_SIZE]} ({self.current_doc[Log.PERCENTAGE_REDUCTION]}% reduction)" if all_size_variables else "No size information available",
            f"{'='*30}",
            f"selected: {self.current_doc[Log.SBCUT] if Log.SBCUT in self.current_doc else 'SBCUT not available'}",
            "\n"
        )
        return "\n".join(repres)
        
    def new(self):
        self.current_doc = {}
    
    def current(self, field: Log, value):
        self.current_doc[field] = value

    def write(self):
        with open("log.txt", "a", encoding="utf-8") as f:
            f.write(str(self))
    
    def clear(self):
        with open("log.txt", "w", encoding="utf-8") as f:
            f.write("")
