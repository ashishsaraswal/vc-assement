# hubspot.py

import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
import requests
from integrations.integration_item import IntegrationItem

from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

CLIENT_ID = 'cbb0df1f-17a0-4d4b-ac38-f7de49a664e4'
CLIENT_SECRET = '4c6f8c6c-b402-4e76-b121-930a8be51d1e'
REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
SCOPES = 'crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read'

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode('utf-8')).decode('utf-8')
    
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', json.dumps(state_data), expire=600)
    
    auth_url = f'https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&scope={SCOPES}&redirect_uri={REDIRECT_URI}&state={encoded_state}'
    return auth_url

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error_description'))
    
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode('utf-8'))
    
    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')
    
    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    
    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')
    
    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                'https://api.hubapi.com/oauth/v1/token',
                data={
                    'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'code': code
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )
    
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=600)
    
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    credentials = json.loads(credentials)
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')
    return credentials

def create_integration_item_metadata_object(response_json, item_type):
    properties = response_json.get('properties', {})
    
    # Extract name based on item type
    if item_type == 'Contact':
        name = f"{properties.get('firstname', '')} {properties.get('lastname', '')}".strip() or properties.get('email', 'Unnamed Contact')
    elif item_type == 'Company':
        name = properties.get('name', 'Unnamed Company')
    elif item_type == 'Deal':
        name = properties.get('dealname', 'Unnamed Deal')
    else:
        name = properties.get('name', 'Unnamed')
    
    integration_item_metadata = IntegrationItem(
        id=str(response_json.get('id')),
        name=name,
        type=item_type,
        creation_time=response_json.get('createdAt'),
        last_modified_time=response_json.get('updatedAt'),
        url=f"https://app.hubspot.com/contacts/{response_json.get('id')}"
    )
    return integration_item_metadata

async def get_items_hubspot(credentials):
    credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    
    list_of_integration_item_metadata = []
    
    print("\n=== HubSpot Integration - Fetching Data ===")
    
    # Get contacts with properties
    print("Fetching contacts...")
    contacts_response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/contacts?properties=firstname,lastname,email,phone,company',
        headers=headers
    )
    
    if contacts_response.status_code == 200:
        contacts = contacts_response.json().get('results', [])
        print(f"Found {len(contacts)} contacts")
        for contact in contacts:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(contact, 'Contact')
            )
    else:
        print(f"Failed to fetch contacts: {contacts_response.status_code}")
    
    # Get companies with properties
    print("Fetching companies...")
    companies_response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/companies?properties=name,domain,industry,city,state',
        headers=headers
    )
    
    if companies_response.status_code == 200:
        companies = companies_response.json().get('results', [])
        print(f"Found {len(companies)} companies")
        for company in companies:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(company, 'Company')
            )
    else:
        print(f"Failed to fetch companies: {companies_response.status_code}")
    
    # Get deals with properties
    print("Fetching deals...")
    deals_response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/deals?properties=dealname,amount,dealstage,closedate',
        headers=headers
    )
    
    if deals_response.status_code == 200:
        deals = deals_response.json().get('results', [])
        print(f"Found {len(deals)} deals")
        for deal in deals:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(deal, 'Deal')
            )
    else:
        print(f"Failed to fetch deals: {deals_response.status_code}")
    
    print(f"\nTotal items retrieved: {len(list_of_integration_item_metadata)}")
    print("\n=== Integration Items ===")
    for item in list_of_integration_item_metadata:
        print(f"ID: {item.id}, Type: {item.type}, Name: {item.name}")
    print("========================\n")
    

    return [item.to_dict() for item in list_of_integration_item_metadata]