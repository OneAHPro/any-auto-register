from fastapi import FastAPI
from fastapi.testclient import TestClient


def client_for(root):
    from services.frontend_static import frontend_response
    app=FastAPI()
    @app.api_route('/{path:path}',methods=['GET','HEAD'])
    def frontend(path): return frontend_response(root,path)
    return TestClient(app)


def test_fonts_and_favicon_are_files_not_spa_html(tmp_path):
    (tmp_path/'index.html').write_text('<html>console</html>')
    (tmp_path/'fonts').mkdir()
    (tmp_path/'fonts/TCloudNumberVF.ttf').write_bytes(b'font-fixture')
    (tmp_path/'console-mark.svg').write_text('<svg/>')
    client=client_for(tmp_path)
    font=client.get('/fonts/TCloudNumberVF.ttf')
    assert font.status_code==200
    assert font.content==b'font-fixture'
    assert 'text/html' not in font.headers['content-type']
    assert client.head('/fonts/TCloudNumberVF.ttf').status_code==200
    assert client.get('/console-mark.svg').headers['content-type'].startswith('image/svg+xml')


def test_spa_deep_links_work_but_missing_assets_return_not_found(tmp_path):
    (tmp_path/'index.html').write_text('<html>console</html>')
    client=client_for(tmp_path)
    assert client.get('/accounts/chatgpt').text=='<html>console</html>'
    assert client.get('/supply').status_code==200
    assert client.get('/fonts/missing.ttf').status_code==404


def test_static_path_and_symlinks_cannot_leave_public_directory(tmp_path):
    root=tmp_path/'static'; root.mkdir()
    (root/'index.html').write_text('console')
    secret=tmp_path/'private.txt'; secret.write_text('private-fixture')
    (root/'outside.txt').symlink_to(secret)
    client=client_for(root)
    assert client.get('/%2e%2e/private.txt').status_code==404
    assert client.get('/outside.txt').status_code==404
