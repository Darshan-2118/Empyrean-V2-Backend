from quart import request, current_app

# Add session middleware to create per-request sessions

@app.before_request
async def bind_request_session():
    if not hasattr(request.app.g, 'request_session_factory'):
        request.app.g.request_session_factory = AsyncSessionLocal

@app.after_request
async def teardown_request(exception):
    session_factory = getattr(request.app.g, "request_session_factory", None)
    if session_factory:
        async with session_factory() as session:
            if not session.in_transaction() and not session.pending_close:
                await session.close()

def get_request_session():
    return request.app.g.request_session_factory