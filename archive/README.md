# Archive

Files moved out of the active project during deployment cleanup (2026-10-19).
Nothing here is imported, built into Docker images, or needed at runtime.
Review and delete when no longer needed (they also remain in git history).

| Folder | Contents |
|---|---|
| `legacy-chat-routers/` | Old chat API modules no longer registered in `backend/app/main.py` |
| `one-off-fixers/` | Scripts that rewrote source files with hard-coded macOS paths. Do not run: `fix_history.py` removes the login check from chat history |
| `duplicate-files/` | Copies of PDFs/DOCX already in `academic-data/` or `backend/advisor/prereq/data/`, and old env templates superseded by `.env.example` |
| `tmp/`, `frontend-test-results/` | Scratch output and Playwright results |
