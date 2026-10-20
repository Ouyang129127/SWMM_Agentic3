"""HTTP adapter. Workflow decisions and task execution belong to orchestration/."""
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from workflow_agents.common.paths import MODELS_DIR, WEB_DIR
from workflow_agents.interfaces.contracts import ChatRequest, ChatResponse
from workflow_agents.orchestration import sessions

app = FastAPI(title='SWMM-Agentic3 Workflow Chat')
if MODELS_DIR.exists():
    app.mount('/models', StaticFiles(directory=str(MODELS_DIR)), name='models')

@app.get('/', response_class=HTMLResponse)
async def index():
    path = WEB_DIR / 'index.html'
    if not path.exists():
        raise HTTPException(status_code=500, detail='Frontend file not found.')
    return HTMLResponse(path.read_text(encoding='utf-8'), headers={'Cache-Control':'no-store, max-age=0'})

@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.post('/api/chat', response_model=ChatResponse)
async def chat(request: ChatRequest):
    return await sessions.chat(request)

@app.get('/api/session/{session_id}', response_model=ChatResponse)
async def get_session(session_id: str):
    return await sessions.get_session(session_id)

@app.delete('/api/session/{session_id}')
async def delete_session(session_id: str):
    return await sessions.delete_session(session_id)
