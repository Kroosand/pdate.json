import pg8000
import sys
import re
import requests
import json
import time
from datetime import datetime

import os

# Reconfigure stdout to UTF-8 to prevent encoding errors on Windows terminal
sys.stdout.reconfigure(encoding='utf-8')

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

# Database credentials (loaded from environment variables to keep them hidden on GitHub)
DB_HOST = os.getenv("BRASIL_DB_HOST")
DB_PORT = int(os.getenv("BRASIL_DB_PORT", "5432"))
DB_NAME = os.getenv("BRASIL_DB_NAME", "postgres")
DB_USER = os.getenv("BRASIL_DB_USER")
DB_PASS = os.getenv("BRASIL_DB_PASS")

if not all([DB_HOST, DB_USER, DB_PASS]):
    raise ValueError("Error: BRASIL_DB_HOST, BRASIL_DB_USER, and BRASIL_DB_PASS environment variables must be defined in your .env file or GitHub Secrets.")

# Regex for allowed alfanumeric prefix IDs (ONU, ONT, ONS followed by digits)
PREFIX_PATTERN = re.compile(r'^(ONU|ONT|ONS)\d+$', re.IGNORECASE)

def get_db_connection():
    return pg8000.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )

def get_wisphub_config(conn):
    cur = conn.cursor()
    cur.execute("SELECT parametro, valor FROM public.config_sistema WHERE parametro IN ('wisphub_api_url', 'wisphub_api_key')")
    rows = cur.fetchall()
    config = {r[0]: r[1] for r in rows}
    cur.close()
    url = config.get('wisphub_api_url', 'https://api.wisphub.net/api/')
    if url and not url.endswith('/'):
        url += '/'
    return url, config.get('wisphub_api_key')

def fetch_wisphub_clients(api_url, api_key, incremental=False):
    headers = {
        'Authorization': f'Api-Key {api_key}',
        'Content-Type': 'application/json'
    }
    
    clients = []
    # If incremental, we only request clients updated today
    url = f"{api_url}clientes/?limit=300"
    if incremental:
        today_str = datetime.now().strftime('%Y-%m-%d')
        url = f"{api_url}clientes/?limit=300&ultimo_cambio={today_str}"
        print(f"[INFO] Running incremental sync for today ({today_str})...")
    else:
        print("[INFO] Running complete sync for all clients...")
        
    while url:
        print(f"Fetching WispHub clients: {url}")
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            print(f"[ERROR] Failed to fetch clients from WispHub: {r.status_code} - {r.text}")
            break
        data = r.json()
        clients.extend(data.get('results', []))
        url = data.get('next')
        if url:
            time.sleep(1.0) # Safe delay between requests
            
    print(f"Fetched {len(clients)} clients from WispHub.")
    return clients

def fetch_wisphub_tickets(api_url, api_key, incremental=False):
    headers = {
        'Authorization': f'Api-Key {api_key}',
        'Content-Type': 'application/json'
    }
    
    tickets = []
    url = f"{api_url}tickets/?limit=300"
    if incremental:
        # Fetch tickets and filter locally
        url = f"{api_url}tickets/?limit=300"
        print("[INFO] Fetching tickets...")
    else:
        print("[INFO] Fetching all tickets...")
        
    while url:
        print(f"Fetching WispHub tickets: {url}")
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            print(f"[ERROR] Failed to fetch tickets from WispHub: {r.status_code} - {r.text}")
            break
        data = r.json()
        tickets.extend(data.get('results', []))
        url = data.get('next')
        if url:
            time.sleep(1.0) # Safe delay
            
    print(f"Fetched {len(tickets)} tickets from WispHub.")
    return tickets

def clean_phone(phone_raw):
    if not phone_raw:
        return ""
    phones = re.findall(r'\+?\d+', str(phone_raw))
    return ",".join(phones)

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

def map_estado(estado_raw):
    val = str(estado_raw or '').strip().upper()
    if val in ('ACTIVO', 'SUSPENDIDO', 'CANCELADO', 'GRATIS'):
        return val
    if val == 'GRATUITO':
        return 'GRATIS'
    return 'ACTIVO'

def floats_close(f1, f2):
    if f1 is None and f2 is None:
        return True
    if f1 is None or f2 is None:
        return False
    return abs(float(f1) - float(f2)) < 1e-5

def run_sync(dry_run=False, incremental=False):
    print("Connecting to ERP Database...")
    conn = get_db_connection()
    
    # 1. Fetch WispHub API credentials from config_sistema
    api_url, api_key = get_wisphub_config(conn)
    if not api_key:
        print("[ERROR] WispHub API key not found in public.config_sistema.")
        conn.close()
        return
        
    # 2. Fetch all ERP clients
    cur = conn.cursor()
    cur.execute("SELECT id_cliente, nombre, telefono, direccion, estado, plan, dni_ruc, id_wisphub, lat, lng FROM public.clientes")
    erp_clients_rows = cur.fetchall()
    desc_cli = [d[0] for d in cur.description]
    erp_clients = [dict(zip(desc_cli, r)) for r in erp_clients_rows]
    
    # Filter ERP clients to keep prefix alfanumeric (ONU/ONT/ONS) and EXCLUDE purely numeric ones
    erp_by_id = {}
    erp_by_wisphub = {}
    excluded_numeric_count = 0
    for cli in erp_clients:
        id_str = str(cli['id_cliente']).strip()
        if PREFIX_PATTERN.match(id_str):
            erp_by_id[id_str.upper()] = cli
            wh_id = str(cli['id_wisphub'] or '').strip()
            if wh_id:
                erp_by_wisphub[wh_id] = cli
        else:
            excluded_numeric_count += 1
            
    print(f"Total clients in ERP: {len(erp_clients)}")
    print(f"  - Active prefix-based (ONU/ONT/ONS): {len(erp_by_id)}")
    print(f"  - Excluded numeric/other IDs: {excluded_numeric_count}")
    
    # 3. Fetch WispHub Clients
    wh_clients = fetch_wisphub_clients(api_url, api_key, incremental=incremental)
    
    # Track stats
    stats = {
        'inserted': 0,
        'updated': 0,
        'cancelled': 0,
        'skipped_numeric': 0,
        'skipped_inactive': 0
    }
    
    # Keep track of matched ERP client IDs
    matched_erp_ids = set()
    
    # 4. Sync Clients
    for wh in wh_clients:
        wh_serv = str(wh.get('servicio') or '').strip()
        wh_user = str(wh.get('usuario') or '').strip().split('@')[0]
        
        # Check if the service matches the prefix pattern
        is_prefix_match = PREFIX_PATTERN.match(wh_serv) or PREFIX_PATTERN.match(wh_user)
        if not is_prefix_match:
            stats['skipped_numeric'] += 1
            continue
            
        # Determine the unique ID to match
        matched_id = None
        erp_cli = None
        wh_serv_up = wh_serv.upper()
        wh_user_up = wh_user.upper()
        
        if wh_serv_up in erp_by_id:
            matched_id = wh_serv_up
            erp_cli = erp_by_id[wh_serv_up]
        elif wh_user_up in erp_by_id:
            matched_id = wh_user_up
            erp_cli = erp_by_id[wh_user_up]
            
        wh_estado = str(wh.get('estado') or 'Activo').strip()
        wh_nombre = str(wh.get('nombre') or '').strip()
        wh_telefono = clean_phone(wh.get('telefono'))
        wh_direccion = str(wh.get('direccion') or '').strip()
        wh_barrio = str(wh.get('localidad') or '').strip()
        if wh_barrio:
            wh_direccion = f"{wh_direccion} - {wh_barrio}" if wh_direccion else wh_barrio
            
        wh_plan = str(wh.get('precio_plan') or '').strip()
        wh_dni = str(wh.get('cedula') or '').strip()
        wh_id_wisphub = str(wh.get('id_servicio') or '').strip()
        wh_lat, wh_lng = parse_coordinates(wh.get('coordenadas'))
        
        target_estado = map_estado(wh_estado)
        
        if erp_cli:
            # Client exists -> Check if updates are needed
            matched_erp_ids.add(matched_id)
            erp_estado = str(erp_cli['estado'] or '').strip().upper()
            
            # Compare fields to avoid redundant updates
            has_changes = (
                wh_nombre != str(erp_cli['nombre'] or '').strip() or
                wh_telefono != str(erp_cli['telefono'] or '').strip() or
                wh_direccion != str(erp_cli['direccion'] or '').strip() or
                wh_plan != str(erp_cli['plan'] or '').strip() or
                wh_dni != str(erp_cli['dni_ruc'] or '').strip() or
                wh_id_wisphub != str(erp_cli['id_wisphub'] or '').strip() or
                target_estado != erp_estado or
                not floats_close(wh_lat, erp_cli.get('lat')) or
                not floats_close(wh_lng, erp_cli.get('lng'))
            )
            if has_changes:
                print(f"[ACTION] UPDATE: Client {erp_cli['id_cliente']} ({wh_nombre}) information will be updated.")
                stats['updated'] += 1
                if not dry_run:
                    cur.execute(
                        """UPDATE public.clientes 
                           SET nombre = %s, telefono = %s, direccion = %s, plan = %s, dni_ruc = %s, id_wisphub = %s, estado = %s, lat = %s, lng = %s, updated_at = CURRENT_TIMESTAMP
                           WHERE id_cliente = %s""",
                        (wh_nombre, wh_telefono, wh_direccion, wh_plan, wh_dni, wh_id_wisphub, target_estado, wh_lat, wh_lng, erp_cli['id_cliente'])
                    )
        else:
            # Client does not exist -> Insert if active in WispHub
            if wh_estado.upper() == 'ACTIVO':
                new_id = wh_serv if PREFIX_PATTERN.match(wh_serv) else wh_user
                print(f"[ACTION] INSERT: Client {new_id} ({wh_nombre}) will be imported into ERP.")
                stats['inserted'] += 1
                if not dry_run:
                    cur.execute(
                        """INSERT INTO public.clientes (id_cliente, nombre, telefono, direccion, estado, plan, dni_ruc, id_wisphub, lat, lng)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (new_id, wh_nombre, wh_telefono, wh_direccion, target_estado, wh_plan, wh_dni, wh_id_wisphub, wh_lat, wh_lng)
                    )
            else:
                stats['skipped_inactive'] += 1
                
        # Update memory maps for subsequent ticket sync
        if wh_id_wisphub:
            if erp_cli:
                # Update existing client dict in place to preserve all other fields (like 'estado')
                erp_cli['id_wisphub'] = wh_id_wisphub
                erp_cli['nombre'] = wh_nombre
                erp_cli['lat'] = wh_lat
                erp_cli['lng'] = wh_lng
                erp_by_wisphub[wh_id_wisphub] = erp_cli
            elif wh_estado.upper() == 'ACTIVO':
                # Create a new full dict for the newly inserted active client
                client_id_val = wh_serv if PREFIX_PATTERN.match(wh_serv) else wh_user
                new_client_dict = {
                    'id_cliente': client_id_val,
                    'nombre': wh_nombre,
                    'id_wisphub': wh_id_wisphub,
                    'estado': 'ACTIVO',
                    'telefono': wh_telefono,
                    'direccion': wh_direccion,
                    'plan': wh_plan,
                    'dni_ruc': wh_dni,
                    'lat': wh_lat,
                    'lng': wh_lng
                }
                erp_by_id[client_id_val.upper()] = new_client_dict
                erp_by_wisphub[wh_id_wisphub] = new_client_dict

                
    # 5. Sync Tickets (OTs)
    wh_tickets = fetch_wisphub_tickets(api_url, api_key, incremental=incremental)
    
    stats_ot = {'inserted': 0, 'updated': 0, 'skipped': 0}
    
    # Fetch existing OTs and build memory indexes
    cur.execute("SELECT id_ot, id_cliente, estado, id_wisphub, descripcion, tipo FROM public.ordenes_trabajo")
    existing_ots_rows = cur.fetchall()
    
    existing_ots_by_wh = {}
    for r in existing_ots_rows:
        ot_id, id_cli, est, id_wh, desc, tip = r
        if id_wh:
            existing_ots_by_wh[str(id_wh).strip()] = r
            
    # Calculate next sequence number for OT format: OT-YYYY-N
    year = datetime.now().year
    max_seq = 0
    for r in existing_ots_rows:
        ot_id = str(r[0])
        parts = ot_id.split('-')
        if len(parts) == 3 and parts[0] == 'OT' and parts[1] == str(year):
            try:
                seq = int(parts[2])
                if seq > max_seq:
                    max_seq = seq
            except ValueError:
                pass
    next_seq = max_seq + 1
    
    for tk in wh_tickets:
        tk_id = str(tk.get('id_ticket') or '').strip()
        if not tk_id:
            continue
            
        # Only process open tickets (Nuevo or En Proceso) from WispHub
        tk_status = str(tk.get('estado') or '').strip()
        if tk_status not in ('Nuevo', 'En Proceso'):
            continue
            
        # First, retrieve the client WispHub ID and username/service name
        serv_obj = tk.get('servicio') or {}
        wh_client_id = str(serv_obj.get('id_servicio') or '').strip()
        tk_user = str(serv_obj.get('servicio') or serv_obj.get('usuario') or '').strip().split('@')[0]
        
        client_id_found = None
        
        # 1. Match by WispHub Client ID
        if wh_client_id and wh_client_id in erp_by_wisphub:
            client_id_found = erp_by_wisphub[wh_client_id]['id_cliente']
            
        # 2. Match by username/service if it has the prefix
        if not client_id_found:
            if tk_user and PREFIX_PATTERN.match(tk_user):
                tk_user_up = tk_user.upper()
                if tk_user_up in erp_by_id:
                    client_id_found = erp_by_id[tk_user_up]['id_cliente']
                    
        # 3. Match by description/subject search for prefix
        if not client_id_found:
            desc_text = str(tk.get('asunto') or '') + " " + str(tk.get('detalle') or '')
            prefix_match = PREFIX_PATTERN.search(desc_text)
            if prefix_match:
                candidate_id = prefix_match.group(0).upper()
                if candidate_id in erp_by_id:
                    client_id_found = erp_by_id[candidate_id]['id_cliente']
                    
        # Skip tickets of numeric or non-prefix clients
        if not client_id_found:
            stats_ot['skipped'] += 1
            continue
            
        # Helper function to strip HTML tags
        def strip_html(text):
            if not text:
                return ""
            return re.sub(r'<[^>]*>', '', str(text))
 
        # Smart parse type and description from WispHub ticket
        import html
        import unicodedata
        
        asunto = tk.get('asunto') or ''
        desc_html = tk.get('descripcion') or ''
        
        default_tipo = str(asunto or 'SOPORTE').strip().upper()
        
        # 1. Unescape HTML entities
        plain_desc = html.unescape(str(desc_html))
        # 2. Replace block tags with newlines
        plain_desc = re.sub(r'</p>|</div>|<br\s*/?>', '\n', plain_desc, flags=re.IGNORECASE)
        plain_desc = re.sub(r'<[^>]*>', '', plain_desc)
        
        # 3. Split by lines and clean
        desc_lines = []
        for line in plain_desc.split('\n'):
            cleaned_line = line.strip()
            if cleaned_line:
                desc_lines.append(cleaned_line)
                
        # Helper to normalize strings for comparison
        def clean_compare(text):
            text_norm = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
            return re.sub(r'[^a-z0-9]', '', text_norm.lower())
            
        asunto_clean = clean_compare(asunto)
        first_line_clean = clean_compare(desc_lines[0]) if desc_lines else ""
        
        if len(desc_lines) >= 2 and (asunto_clean in first_line_clean or first_line_clean in asunto_clean):
            ot_tipo = desc_lines[0].upper()
            ot_desc = "\n".join(desc_lines[1:])
        else:
            ot_tipo = default_tipo
            ot_desc = "\n".join(desc_lines) if desc_lines else ""
        
        # Map status: Nuevo/Asignado -> PENDIENTE, En Proceso -> EN_PROCESO, Resuelto/Cerrado -> CERRADA
        tk_status = str(tk.get('estado') or '').strip()
        ot_estado = "PENDIENTE"
        if tk_status == "En Proceso":
            ot_estado = "EN_PROCESO"
        elif tk_status in ("Resuelto", "Cerrado"):
            ot_estado = "CERRADA"
            
        # Standardize date format to "YYYY-MM-DD"
        raw_date = tk.get('fecha_creacion')
        if raw_date:
            if 'T' in raw_date:
                ot_fecha_creacion = raw_date.split('T')[0]
            elif ' ' in raw_date:
                ot_fecha_creacion = raw_date.split(' ')[0]
            else:
                ot_fecha_creacion = raw_date[:10]
        else:
            ot_fecha_creacion = datetime.now().strftime('%Y-%m-%d')
            
        ot_tecnico = str(tk.get('tecnico') or '').strip()
        
        if tk_id in existing_ots_by_wh:
            # Check for changes
            existing_row = existing_ots_by_wh[tk_id]
            ot_id = existing_row[0]
            existing_estado = existing_row[2]
            existing_desc = existing_row[4]
            existing_tipo = existing_row[5]
            
            if (ot_estado != existing_estado or 
                ot_tipo != str(existing_tipo or '').strip().upper() or
                ot_desc != str(existing_desc or '').strip()):
                
                print(f"[ACTION] UPDATE OT: OT {ot_id} (WispHub {tk_id}) will be updated (State: {existing_estado} -> {ot_estado}, Type: {existing_tipo} -> {ot_tipo}).")
                stats_ot['updated'] += 1
                if not dry_run:
                    cur.execute(
                        "UPDATE public.ordenes_trabajo SET estado = %s, tipo = %s, descripcion = %s, updated_at = CURRENT_TIMESTAMP WHERE id_ot = %s",
                        (ot_estado, ot_tipo, ot_desc, ot_id)
                    )
            else:
                stats_ot['skipped'] += 1
        else:
            # Create new OT with standard ERP format: OT-YYYY-Seq
            ot_id = f"OT-{year}-{next_seq}"
            next_seq += 1
            print(f"[ACTION] INSERT OT: OT {ot_id} (WispHub Ticket {tk_id}) for client {client_id_found} will be created.")
            stats_ot['inserted'] += 1
            if not dry_run:
                cur.execute(
                    """INSERT INTO public.ordenes_trabajo (id_ot, id_cliente, tecnico, tipo, estado, fecha_creacion, descripcion, id_wisphub)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (ot_id, client_id_found, None, ot_tipo, ot_estado, ot_fecha_creacion, ot_desc, tk_id)
                )
                
    if not dry_run:
        conn.commit()
        print("\n[SUCCESS] Transaction committed successfully.")
    else:
        print("\n[DRY RUN] Finished simulation. No changes were written to the database.")
        
    print("\n--- FINAL STATISTICS ---")
    print(f"Clients:")
    print(f"  - Imported: {stats['inserted']}")
    print(f"  - Updated:  {stats['updated']}")
    print(f"  - Cancelled:{stats['cancelled']}")
    print(f"  - Skipped (Numeric/No Prefix): {stats['skipped_numeric']}")
    print(f"  - Skipped (Inactive new):      {stats['skipped_inactive']}")
    print(f"Tickets / OTs:")
    print(f"  - Created: {stats_ot['inserted']}")
    print(f"  - Updated: {stats_ot['updated']}")
    print(f"  - Skipped: {stats_ot['skipped']}")
    
    cur.close()
    conn.close()

if __name__ == '__main__':
    dry_run_arg = '--dry-run' in sys.argv
    incremental_arg = '--incremental' in sys.argv
    
    run_sync(dry_run=dry_run_arg, incremental=incremental_arg)
