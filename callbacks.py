from aiogram.filters.callback_data import CallbackData

class MenuCB(CallbackData, prefix="start"):
    action: str  # random, profile, genres, back, clear_disabled

class AnimeCB(CallbackData, prefix="anime"):
    action: str  # watch, like, back_to_list
    id: str      # cb_id

class WatchCB(CallbackData, prefix="watch"):
    action: str  # dummy

class GenreCB(CallbackData, prefix="genre"):
    action: str  # main, mode, toggle, page, clear, back, recommend
    mode: str | None = None  # include, exclude
    slug: str | None = None
    page: int | None = None
    sid: str | None = None # Snapshot ID

class HistoryCB(CallbackData, prefix="hist"):
    action: str # list, show, menu, page, pages
    page: int = 0
    cb_id: str | None = None

class ContentTypeCB(CallbackData, prefix="ct"):
    action: str  # toggle, clear, recommend
    slug: str | None = None

class YearCB(CallbackData, prefix="year"):
    action: str  # open, set_from, set_to, page, clear, recommend
    year: int | None = None
    page: int = 0
    mode: str = "from"  # from, to

class AdminCB(CallbackData, prefix="admin"):
    action: str  # refresh_stats, force_sync

class SeasonCB(CallbackData, prefix="season"):
    action: str  # mode, toggle, clear, recommend
    slug: str | None = None
    mode: str | None = None # include, exclude