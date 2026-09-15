"""Offline portal boundary checks; never contact the university."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cloud_service

info={'student_id':'TEST123','parse_diagnostics':{'complete':True,'fatal':False,'reconciliation':{'status':'reconciled'}}}
with patch('scraper.fetch_transcript',return_value=b'%PDF-test'),patch.object(cloud_service,'parse_transcript_pdf',return_value=(info,[])):
    request={'operation':'portal','account':'TEST123','password':'dummy'}
    response=cloud_service.handle_request(request)
    assert response['status']==200 and response['data']['pdf']
    assert 'password' not in request and 'account' not in request
    assert cloud_service.handle_request({'operation':'portal','account':'OTHER','password':'dummy'})['status']==422
with patch('scraper.fetch_transcript',side_effect=RuntimeError('must not expose dummy')):
    response=cloud_service.handle_request({'operation':'portal','account':'TEST123','password':'dummy'})
    assert response['status']==422 and 'dummy' not in str(response)
print('PASS: portal success, identity mismatch, sanitized failure and credential removal')
