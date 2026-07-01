import os
import re
import time
import uuid
import json
import requests
import psycopg2

# Try to load local .env file if it exists (for local debugging/testing)
if os.path.exists('.env'):
    with open('.env') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                try:
                    key, val = line.split('=', 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")
                except ValueError:
                    pass

def clean_and_format_phones(phone_str):
    if not phone_str:
        return ""
    # Remove all non-digits
    digits_only = re.sub(r'\D', '', phone_str)
    # Find all 9-digit sequences starting with 9 (Peru cell phone standard)
    matches = re.findall(r'9\d{8}', digits_only)
    
    formatted = []
    seen = set()
    for m in matches:
        full_num = f"51{m}"
        if full_num not in seen:
            seen.add(full_num)
            formatted.append(full_num)
    return ",".join(formatted)

def parse_dia_corte(fecha_corte_str):
    if not fecha_corte_str:
        return 7
    try:
        parts = fecha_corte_str.split('/')
        if len(parts) >= 1:
            return int(parts[0])
    except:
        pass
    return 7

def normalize_name(name):
    if not name:
        return ""
    # Convert to lowercase, remove non-alphanumeric/non-space characters, and normalize spaces
    name = name.lower().strip()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()


def parse_coordinates(coords_str):
    if not coords_str:
        return None, None
    try:
        parts = str(coords_str).split(',')
        if len(parts) == 2:
            lat = float(parts[0].strip())
            lng = float(parts[1].strip())
            return lat, lng
    except Exception:
        pass
    return None, None

def floats_close(f1, f2):
    if f1 is None and f2 is None:
        return True
    if f1 is None or f2 is None:
        return False
    return abs(float(f1) - float(f2)) < 1e-5


def get_wisphub_config():
    # 1. First, check if WispHub credentials are provided directly in the environment variables (best practice)
    env_url = os.getenv('WISPHUB_API_URL')
    env_key = os.getenv('WISPHUB_API_KEY')
    if env_url and env_key:
        print("Loaded WispHub configuration directly from environment variables.")
        return {
            'wisphub_api_url': env_url,
            'wisphub_api_key': env_key
        }
        
    # 2. Fallback: Try to load credentials from the Brazil DB using connection variables
    host = os.getenv('BRAZIL_DB_HOST')
    port = os.getenv('BRAZIL_DB_PORT', '6543')
    database = os.getenv('BRAZIL_DB_NAME', 'postgres')
    user = os.getenv('BRAZIL_DB_USER')
    password = os.getenv('BRAZIL_DB_PASS')
    
    config = {
        'wisphub_api_url': '',
        'wisphub_api_key': ''
    }
    
    if not all([host, user, password]):
        raise ValueError("Error: WispHub credentials are not set in environment variables and Brazil DB connection details are missing.")
        
    try:
        conn_str = f"host={host} port={port} dbname={database} user={user} password={password} sslmode=require"
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        cur.execute("SELECT parametro, valor FROM public.config_sistema WHERE parametro IN ('wisphub_api_url', 'wisphub_api_key');")
        for row in cur.fetchall():
            config[row[0]] = row[1]
        cur.close()
        conn.close()
        print("Loaded WispHub config from Brazil DB.")
    except Exception as e:
        print("Error loading config from Brazil DB. Detail:", e)
        
    if not config['wisphub_api_url'] or not config['wisphub_api_key']:
        raise ValueError("Error: Could not resolve WispHub URL or API Key.")
        
    return config

def fetch_wisphub_clients(api_url, api_key):
    headers = {
        'Authorization': f'Api-Key {api_key}',
        'Content-Type': 'application/json'
    }
    
    clients = []
    url = f"{api_url.rstrip('/')}/clientes/?limit=100"
    print(f"Fetching clients from WispHub API: {url}")
    
    while url:
        try:
            r = requests.get(url, headers=headers)
            if r.status_code != 200:
                print(f"Error fetching clients: Status {r.status_code}, Response: {r.text}")
                break
            data = r.json()
            results = data.get('results', [])
            clients.extend(results)
            url = data.get('next')
            if url:
                time.sleep(0.5)  # 0.5s pause to prevent API rate limiting
        except Exception as e:
            print("Exception during WispHub clients fetch:", e)
            break
            
    print(f"Total WispHub clients fetched: {len(clients)}")
    return clients

def fetch_wisphub_unpaid_invoices(api_url, api_key):
    headers = {
        'Authorization': f'Api-Key {api_key}',
        'Content-Type': 'application/json'
    }
    
    invoices = []
    url = f"{api_url.rstrip('/')}/facturas/?estado=1&limit=100" # 1 = Pendiente de Pago
    print(f"Fetching unpaid invoices from WispHub API: {url}")
    
    while url:
        try:
            r = requests.get(url, headers=headers)
            if r.status_code != 200:
                print(f"Error fetching invoices: Status {r.status_code}, Response: {r.text}")
                break
            data = r.json()
            results = data.get('results', [])
            invoices.extend(results)
            url = data.get('next')
            if url:
                time.sleep(0.5)  # 0.5s pause to prevent API rate limiting
        except Exception as e:
            print("Exception during WispHub invoices fetch:", e)
            break
            
    print(f"Total unpaid WispHub invoices fetched: {len(invoices)}")
    return invoices

def run_sync():
    print("--- STARTING WISPHUB DATABASE SYNC ---")
    
    # 1. Get configurations
    config = get_wisphub_config()
    api_url = config['wisphub_api_url']
    api_key = config['wisphub_api_key']
    
    # 2. Fetch data from WispHub
    wh_clients = fetch_wisphub_clients(api_url, api_key)
    wh_invoices = fetch_wisphub_unpaid_invoices(api_url, api_key)
    
    # 3. Group unpaid invoices by id_servicio
    invoices_by_service = {}
    for inv in wh_invoices:
        articulos = inv.get('articulos', [])
        for art in articulos:
            srv = art.get('servicio')
            if srv and 'id_servicio' in srv:
                srv_id = srv['id_servicio']
                invoices_by_service.setdefault(srv_id, []).append(inv)
                
    # 4. Connect to USA Supabase DB REST API (using environment variables)
    url_usa = os.getenv('USA_SUPABASE_URL')
    supabase_key = os.getenv('USA_SUPABASE_KEY')
    
    if not url_usa or not supabase_key:
        print("Error: USA_SUPABASE_URL and USA_SUPABASE_KEY environment variables are missing.")
        return
        
    headers_usa = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json"
    }
    
    # 5. Fetch existing clients and services from Supabase USA with pagination (PostgREST limit/offset)
    print("Fetching existing clients from Supabase USA...")
    existing_clients = []
    limit = 1000
    offset = 0
    while True:
        r_cli = requests.get(f"{url_usa}clientes?limit={limit}&offset={offset}", headers=headers_usa)
        if r_cli.status_code != 200:
            print(f"Error fetching existing clients from Supabase: {r_cli.text}")
            return
        batch = r_cli.json()
        if not batch:
            break
        existing_clients.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    print(f"Total existing clients fetched: {len(existing_clients)}")
    
    print("Fetching existing services from Supabase USA...")
    existing_services = []
    limit = 1000
    offset = 0
    while True:
        r_srv = requests.get(f"{url_usa}servicios?limit={limit}&offset={offset}", headers=headers_usa)
        if r_srv.status_code != 200:
            print(f"Error fetching existing services from Supabase: {r_srv.text}")
            return
        batch = r_srv.json()
        if not batch:
            break
        existing_services.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    print(f"Total existing services fetched: {len(existing_services)}")
    
    # Maps
    existing_cli_by_id = {c['id']: c for c in existing_clients}
    existing_cli_by_dni = {c['dni_ruc']: c for c in existing_clients if c['dni_ruc']}
    existing_srv_by_ident = {s['identificador_sistema']: s for s in existing_services}
    
    # Map Name + Phone combinations AND exact Phones to existing clients
    existing_cli_by_name_phone = {}
    existing_cli_by_phone = {} # --- MEJORA: Diccionario para búsqueda estricta por teléfono
    for c in existing_clients:
        if c.get('telefono'):
            existing_cli_by_phone[c['telefono']] = c # Guardamos teléfono exacto
            
        name_norm = normalize_name(c.get('nombre_completo'))
        if c.get('telefono'):
            for ph in c['telefono'].split(','):
                ph = ph.strip()
                if ph:
                    existing_cli_by_name_phone[(name_norm, ph)] = c
                    existing_cli_by_phone[ph] = c # Guardamos teléfono individual
                    
    # Track items we have processed in this batch to reuse client IDs and prevent duplicates
    batch_cli_by_dni = {}
    batch_cli_by_name_phone = {}
    batch_cli_by_phone = {} # --- MEJORA: Rastreo interno por teléfono
    processed_client_ids = set()
    
    clients_payload = []
    services_payload = []
    
    new_clients_count = 0
    updated_clients_count = 0
    new_services_count = 0
    updated_services_count = 0
    
    # 6. Process each WispHub client
    for c in wh_clients:
        id_servicio = c.get('id_servicio')
        if not id_servicio:
            continue
            
        # Filter: Only active services
        wh_estado = str(c.get('estado') or '').upper()
        if wh_estado != "ACTIVO":
            continue
            
        # Filter: Only services with pending payments (unpaid invoices)
        if id_servicio not in invoices_by_service:
            continue
            
        identificador_sistema = f"INT-{id_servicio}"
        nombre_completo = c.get('nombre') or ""
        # Clean trailing service indicators like "Juan Perez 1", "Juan Perez 2", etc.
        nombre_completo = re.sub(r'\s+[-–]?\s*\d+$', '', nombre_completo).strip()
        dni_ruc = c.get('cedula')
        if dni_ruc:
            dni_ruc = str(dni_ruc).strip()
        if not dni_ruc:
            dni_ruc = None
            
        telefono_raw = c.get('telefono') or ""
        telefono_clean = clean_and_format_phones(telefono_raw)
        if not telefono_clean:
            # Skip clients without a phone number because:
            # 1. The database enforces a NOT NULL constraint on "telefono".
            # 2. A WhatsApp bot cannot interact with clients who don't have a phone number.
            print(f"Skipping service {id_servicio} ({nombre_completo}) - No phone number.")
            continue
            
        phones_list = [p.strip() for p in telefono_clean.split(',') if p.strip()]
        
        # Check if service already exists
        existing_srv = existing_srv_by_ident.get(identificador_sistema)
        
        client_id = None
        bot_activo = True  # Default for new clients
        
        if existing_srv:
            # Service exists -> link to its existing client ID
            client_id = existing_srv['cliente_id']
            # Fetch existing client to preserve bot_activo state
            existing_cli = existing_cli_by_id.get(client_id)
            if existing_cli:
                bot_activo = existing_cli.get('bot_activo', True)
            updated_services_count += 1
        else:
            # Service does not exist -> check if client already exists (by DNI or Name + Phone)
            existing_cli = None
            
            # A. Match by DNI in database
            if dni_ruc and dni_ruc in existing_cli_by_dni:
                existing_cli = existing_cli_by_dni[dni_ruc]
                
            # B. Match by DNI in current batch
            elif dni_ruc and dni_ruc in batch_cli_by_dni:
                client_id = batch_cli_by_dni[dni_ruc]
                for p_cli in clients_payload:
                    if p_cli['id'] == client_id:
                        bot_activo = p_cli['bot_activo']
                        break
                        
            # C. Match by Name + Phone in database
            if not existing_cli and not client_id:
                name_norm = normalize_name(nombre_completo)
                for ph in phones_list:
                    if (name_norm, ph) in existing_cli_by_name_phone:
                        existing_cli = existing_cli_by_name_phone[(name_norm, ph)]
                        break
                        
            # D. Match by Name + Phone in current batch
            if not existing_cli and not client_id:
                name_norm = normalize_name(nombre_completo)
                for ph in phones_list:
                    if (name_norm, ph) in batch_cli_by_name_phone:
                        client_id = batch_cli_by_name_phone[(name_norm, ph)]
                        for p_cli in clients_payload:
                            if p_cli['id'] == client_id:
                                bot_activo = p_cli['bot_activo']
                                break
                        break

            # --- MEJORA: E. Match by Phone ONLY en Base de Datos ---
            # Soluciona el error 409 forzando la reutilización del ID si el teléfono ya existe
            if not existing_cli and not client_id:
                if telefono_clean in existing_cli_by_phone:
                    existing_cli = existing_cli_by_phone[telefono_clean]
                else:
                    for ph in phones_list:
                        if ph in existing_cli_by_phone:
                            existing_cli = existing_cli_by_phone[ph]
                            break
                            
            # --- MEJORA: F. Match by Phone ONLY en el Lote Actual ---
            if not existing_cli and not client_id:
                if telefono_clean in batch_cli_by_phone:
                    client_id = batch_cli_by_phone[telefono_clean]
                else:
                    for ph in phones_list:
                        if ph in batch_cli_by_phone:
                            client_id = batch_cli_by_phone[ph]
                            break
                # Si encontramos el ID, recuperamos el estado del bot
                if client_id:
                    for p_cli in clients_payload:
                        if p_cli['id'] == client_id:
                            bot_activo = p_cli['bot_activo']
                            break
            
            # Resolve ID and counters
            if existing_cli:
                client_id = existing_cli['id']
                bot_activo = existing_cli.get('bot_activo', True)
                updated_clients_count += 1
            elif not client_id:
                client_id = str(uuid.uuid4())
                new_clients_count += 1
            else:
                updated_clients_count += 1
                
            new_services_count += 1
            
        # Register in batch maps to prevent duplicates in future iterations of this loop
        if dni_ruc:
            batch_cli_by_dni[dni_ruc] = client_id
        name_norm = normalize_name(nombre_completo)
        
        batch_cli_by_phone[telefono_clean] = client_id # --- MEJORA: Registrar teléfono exacto
        
        for ph in phones_list:
            batch_cli_by_name_phone[(name_norm, ph)] = client_id
            batch_cli_by_phone[ph] = client_id # --- MEJORA: Registrar teléfono individual
            
        # Create client payload row if not already added in this sync batch
        if client_id not in processed_client_ids:
            processed_client_ids.add(client_id)
            
            # Combine address and localidad (district/barrio)
            barrio = c.get('localidad') or ""
            
            direccion_clean = c.get('direccion') or ""
            if barrio:
                direccion_clean = f"{direccion_clean} - {barrio}" if direccion_clean else barrio
            
            wh_lat, wh_lng = parse_coordinates(c.get('coordenadas'))
            
            # Safe merge rules to prevent overwriting existing database fields with null/empty values
            if existing_cli:
                # 1. Protect name
                if not nombre_completo and existing_cli.get('nombre_completo'):
                    nombre_completo = existing_cli['nombre_completo']
                # 2. Protect DNI
                if not dni_ruc and existing_cli.get('dni_ruc'):
                    dni_ruc = existing_cli['dni_ruc']
                # 3. Protect phone
                if not telefono_clean and existing_cli.get('telefono'):
                    telefono_clean = existing_cli['telefono']
                # 4. Protect address
                if not c.get('direccion') and existing_cli.get('direccion'):
                    direccion_clean = existing_cli['direccion']
                # 5. Protect coordinates if empty in WispHub
                if wh_lat is None and existing_cli.get('lat') is not None:
                    wh_lat = existing_cli['lat']
                if wh_lng is None and existing_cli.get('lng') is not None:
                    wh_lng = existing_cli['lng']
            
            clients_payload.append({
                "id": client_id,
                "nombre_completo": nombre_completo,
                "dni_ruc": dni_ruc,
                "telefono": telefono_clean,
                "bot_activo": bot_activo
            })

        else:
            # Merge any new phone numbers from other services of the same client
            for p_cli in clients_payload:
                if p_cli['id'] == client_id:
                    existing_phones = [p.strip() for p in (p_cli['telefono'] or '').split(',') if p.strip()]
                    new_phones = [p for p in phones_list if p not in existing_phones]
                    if new_phones:
                        combined_phones = existing_phones + new_phones
                        p_cli['telefono'] = ",".join(combined_phones)
                    break
            
        # Determine payment details
        # If there are unpaid invoices, sum their total to establish exact debt
        service_invoices = invoices_by_service.get(id_servicio, [])
        if service_invoices:
            total_debt = sum(float(inv.get('total') or 0.0) for inv in service_invoices)
            estado_pago = "PENDIENTE"
            monto_mensual = total_debt
        else:
            estado_pago = "PAGADO"
            monto_mensual = float(c.get('precio_plan') or 0.0)
            
        dia_corte = parse_dia_corte(c.get('fecha_corte'))
        
        # Estado servicio: normalize Activo -> ACTIVO, other -> SUSPENDIDO
        wh_estado = str(c.get('estado') or '').upper()
        estado_servicio = "ACTIVO" if wh_estado == "ACTIVO" else "SUSPENDIDO"
        
        # Prepare service fields preserving metadata if existing
        srv_id = existing_srv['id'] if existing_srv else str(uuid.uuid4())
        abono = float(existing_srv.get('abono') or 0.0) if existing_srv else 0.0
        intentos = existing_srv.get('intentos_activacion') if existing_srv else None
        
        services_payload.append({
            "id": srv_id,
            "cliente_id": client_id,
            "categoria": "INTERNET",  # Forced INTERNET for WispHub
            "identificador_sistema": identificador_sistema,  # Forced INT- prefix
            "monto_mensual": monto_mensual,
            "dia_corte": dia_corte,
            "estado_servicio": estado_servicio,
            "estado_pago": estado_pago,
            "abono": abono,
            "intentos_activacion": intentos
        })
        
    # 7. Upsert data to Supabase
    if clients_payload:
        print(f"Upserting {len(clients_payload)} clients to Supabase USA...")
        headers_upsert = headers_usa.copy()
        headers_upsert["Prefer"] = "resolution=merge-duplicates"
        r_up_cli = requests.post(url_usa + "clientes", json=clients_payload, headers=headers_upsert)
        
        # Bucle de seguridad para limpieza dinámica (Corrección preservada)
        max_retries = 4
        retries = 0
        
        while r_up_cli.status_code == 400 and ('pgrst204' in r_up_cli.text.lower() or 'column' in r_up_cli.text.lower()) and retries < max_retries:
            msg = r_up_cli.text.lower()
            print(f"[INFO] La base de datos no soporta una columna. Limpiando payload y reintentando (Intento {retries + 1})...")
            
            col_to_remove = None
            if 'direccion' in msg: col_to_remove = 'direccion'
            elif 'id_wisphub' in msg: col_to_remove = 'id_wisphub'
            elif 'lat' in msg: col_to_remove = 'lat'
            elif 'lng' in msg: col_to_remove = 'lng'
            else:
                col_to_remove = 'todas'
                unsupported_cols = ['direccion', 'id_wisphub', 'lat', 'lng']
                for cli in clients_payload:
                    for col in unsupported_cols:
                        cli.pop(col, None)

            if col_to_remove and col_to_remove != 'todas':
                for cli in clients_payload:
                    cli.pop(col_to_remove, None)
            
            r_up_cli = requests.post(url_usa + "clientes", json=clients_payload, headers=headers_upsert)
            retries += 1
        
        if r_up_cli.status_code not in (200, 201):
            try:
                detail_msg = r_up_cli.json()
            except Exception:
                detail_msg = {"message": r_up_cli.text}
                
            code = detail_msg.get('code') if isinstance(detail_msg, dict) else None
            msg_str = str(detail_msg)
            
            # Check for VARCHAR(20) overflow error: code "22001" or message contents
            if code == '22001' or 'character varying(20)' in msg_str or 'too long' in msg_str:
                print("\n[WARNING] The 'telefono' column in Supabase 'clientes' table is restricted to 20 characters (VARCHAR(20)).")
                print("Multiple numbers cannot be stored. To support multiple numbers, please run the following SQL command in your Supabase dashboard:")
                print("  ALTER TABLE public.clientes ALTER COLUMN telefono TYPE VARCHAR(100);\n")
                print("Retrying client sync by keeping only the first phone number for each client to prevent failures...")
                
                # Truncate telefono field in payload to the first number only
                for cli in clients_payload:
                    if cli['telefono']:
                        cli['telefono'] = cli['telefono'].split(',')[0]
                
                # Retry upsert
                r_up_cli = requests.post(url_usa + "clientes", json=clients_payload, headers=headers_upsert)
                if r_up_cli.status_code not in (200, 201):
                    print(f"Retry failed: Status {r_up_cli.status_code}, Detail: {r_up_cli.text}")
                    return
                else:
                    print("Client sync succeeded on retry with first phone numbers.")
            else:
                print(f"Error upserting clients to Supabase: Status {r_up_cli.status_code}, Detail: {detail_msg}")
                return
            
    if services_payload:
        print(f"Upserting {len(services_payload)} services to Supabase USA...")
        headers_upsert = headers_usa.copy()
        headers_upsert["Prefer"] = "resolution=merge-duplicates"
        r_up_srv = requests.post(url_usa + "servicios", json=services_payload, headers=headers_upsert)
        if r_up_srv.status_code not in (200, 201):
            print(f"Error upserting services to Supabase: Status {r_up_srv.status_code}, Detail: {r_up_srv.text}")
            return
            
    print("\n--- SYNC COMPLETED SUCCESSFULLY ---")
    print(f"Clients: {new_clients_count} created, {updated_clients_count} updated.")
    print(f"Services: {new_services_count} created, {updated_services_count} updated.")
    print(f"Total rows processed: {len(clients_payload)} clients and {len(services_payload)} services.")

if __name__ == '__main__':
    run_sync()
