#!/usr/bin/env python3
import os
import json
import sqlite3
import xml.etree.ElementTree as ET
import shutil
import tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
import time
import hashlib
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import asyncio
import websockets

# WebSocket client management
websocket_clients = set()

async def websocket_handler(websocket):
    """Handle WebSocket connections"""
    websocket_clients.add(websocket)
    print(f"WebSocket client connected. Total clients: {len(websocket_clients)}")
    try:
        await websocket.wait_closed()
    finally:
        websocket_clients.remove(websocket)
        print(f"WebSocket client disconnected. Total clients: {len(websocket_clients)}")

async def broadcast_update(message):
    """Broadcast update to all connected WebSocket clients"""
    if websocket_clients:
        print(f"Broadcasting update to {len(websocket_clients)} clients: {message}")
        disconnected = set()
        for client in websocket_clients:
            try:
                await client.send(json.dumps(message))
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(client)
        
        # Remove disconnected clients
        websocket_clients.difference_update(disconnected)

class TTMLWatcher(FileSystemEventHandler):
    def __init__(self, server_instance):
        self.server_instance = server_instance
        self.processing = False
        
    def on_created(self, event):
        if hasattr(event, 'is_directory') and not event.is_directory and event.src_path.endswith('.ttml'):
            print(f"New TTML file detected: {event.src_path}")
            self.schedule_cache_update(event.src_path)
    
    def on_modified(self, event):
        if hasattr(event, 'is_directory') and not event.is_directory and event.src_path.endswith('.ttml'):
            print(f"TTML file modified: {event.src_path}")
            self.schedule_cache_update(event.src_path)
    
    def schedule_cache_update(self, file_path):
        if self.processing:
            return
        
        # Wait a bit to ensure file is fully written
        def update_cache():
            self.processing = True
            time.sleep(2)  # Wait for file to be fully written
            try:
                self.server_instance.process_new_ttml_file(file_path)
                # Notify WebSocket clients of the update
                try:
                    loop = None
                    for thread in threading.enumerate():
                        if hasattr(thread, '_target') and thread._target and 'websocket' in str(thread._target):
                            # Find the WebSocket thread's event loop
                            break
                    
                    # Simple notification without event loop dependency
                    if websocket_clients:
                        print(f"New TTML file processed: {file_path} - {len(websocket_clients)} clients will be notified")
                        # Create a simple thread to handle WebSocket notifications
                        def notify_clients():
                            try:
                                import asyncio
                                asyncio.run(broadcast_update({
                                    'type': 'podcast_updated',
                                    'message': 'New podcast detected - refreshing data...'
                                }))
                            except Exception as e:
                                print(f"WebSocket notification error: {e}")
                        
                        notification_thread = threading.Thread(target=notify_clients)
                        notification_thread.daemon = True
                        notification_thread.start()
                except Exception as ws_error:
                    print(f"WebSocket notification error: {ws_error}")
            except Exception as e:
                print(f"Error processing new TTML file: {e}")
            finally:
                self.processing = False
        
        thread = threading.Thread(target=update_cache)
        thread.daemon = True
        thread.start()

class PodcastRequestHandler(SimpleHTTPRequestHandler):
    CACHE_FILE = 'podcast_cache.json'
    TTML_CACHE_DIR = 'cached_ttml'
    
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == '/api/podcasts-cached':
            self.send_cached_podcasts()
        else:
            # Serve static files normally
            super().do_GET()
    
    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == '/api/delete-episode':
            self.delete_episode()
        else:
            self.send_error(404, "Not found")
    
    def load_cache(self):
        """Load cached podcast data from file"""
        if os.path.exists(self.CACHE_FILE):
            try:
                with open(self.CACHE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading cache: {e}")
        return {}
    
    def save_cache(self, cache_data):
        """Save podcast data to cache file"""
        try:
            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            print(f"Cache saved to {self.CACHE_FILE}")
        except Exception as e:
            print(f"Error saving cache: {e}")
    
    def get_file_hash(self, file_path):
        """Get MD5 hash of file for change detection"""
        hash_md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception:
            return None
    
    def process_new_ttml_file(self, source_path):
        """Process a newly detected TTML file and update cache"""
        try:
            print(f"Processing new TTML file: {source_path}")
            
            # Ensure cache directory exists
            os.makedirs(self.TTML_CACHE_DIR, exist_ok=True)
            
            # Get file hash
            file_hash = self.get_file_hash(source_path)
            if not file_hash:
                print("  Could not get file hash")
                return
            
            # Load existing cache
            cache = self.load_cache()
            cache_key = f"ttml_{file_hash}"
            
            # Check if already cached
            if cache_key in cache:
                print("  File already in cache")
                return
            
            # Check file size
            file_size = os.path.getsize(source_path)
            if file_size > 1000000:  # 1MB limit
                print(f"  Skipping large file ({file_size} bytes)")
                return
            
            # Copy to cache directory
            filename = os.path.basename(source_path)
            cached_filename = f"{file_hash}_{filename}"
            cached_file_path = os.path.join(self.TTML_CACHE_DIR, cached_filename)
            
            shutil.copy2(source_path, cached_file_path)
            print(f"  Copied to cache: {cached_filename}")
            
            # Parse the file
            parsed = self.parse_ttml_file(cached_file_path)
            if parsed and parsed['chunks']:
                # Store metadata in cache
                metadata = {
                    'id': parsed['id'],
                    'lastModified': parsed['lastModified'],
                    'chunk_count': len(parsed['chunks'])
                }
                cache[cache_key] = metadata
                
                # Save updated cache
                self.save_cache(cache)
                print(f"  Successfully cached: {parsed['id']} with {len(parsed['chunks'])} chunks")
                
                # Try to get database metadata for this podcast
                self.update_podcast_metadata(parsed['id'], file_hash)
            
        except Exception as e:
            print(f"Error processing new TTML file {source_path}: {e}")
    
    def update_podcast_metadata(self, podcast_id, file_hash):
        """Update podcast metadata from database"""
        try:
            podcast_base = os.path.expanduser('~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts')
            db_source_path = os.path.join(podcast_base, 'Documents/MTLibrary.sqlite')
            
            if not os.path.exists(db_source_path):
                return
            
            with tempfile.TemporaryDirectory() as temp_dir:
                # Copy database to temp directory
                db_path = os.path.join(temp_dir, 'MTLibrary.sqlite')
                shutil.copy2(db_source_path, db_path)
                
                # Also copy WAL file if it exists
                wal_source = db_source_path + '-wal'
                if os.path.exists(wal_source):
                    shutil.copy2(wal_source, db_path + '-wal')
                
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                # Get metadata for this specific podcast
                query = """
                SELECT e.ZSTORETRACKID, e.ZAUTHOR, e.ZCLEANEDTITLE, e.ZITUNESSUBTITLE, e.ZDURATION,
                       p.ZTITLE as PODCAST_TITLE, p.ZAUTHOR as PODCAST_AUTHOR, p.ZWEBPAGEURL, 
                       p.ZFEEDURL, p.ZITEMDESCRIPTION as PODCAST_DESCRIPTION, p.ZIMAGEURL, p.ZCATEGORY,
                       e.ZFIRSTTIMEAVAILABLE
                FROM ZMTEPISODE e
                LEFT JOIN ZMTPODCAST p ON e.ZPODCAST = p.Z_PK
                WHERE CAST(e.ZSTORETRACKID AS TEXT) = ?
                """
                cursor.execute(query, [podcast_id])
                row = cursor.fetchone()
                
                if row:
                    metadata = {
                        'author': row[1] or 'Unknown',
                        'title': row[2] or 'Unknown',
                        'description': row[3] or '',
                        'duration': row[4] or -1,
                        'time': (row[12] + 978307200) if row[12] else -1,
                        'podcast_title': row[5] or 'Unknown',
                        'podcast_author': row[6] or 'Unknown',
                        'podcast_url': row[7] or '',
                        'podcast_feed_url': row[8] or '',
                        'podcast_description': row[9] or '',
                        'podcast_image_url': row[10] or '',
                        'podcast_category': row[11] or ''
                    }
                    
                    # Update cache with metadata
                    cache = self.load_cache()
                    cache_key = f"ttml_{file_hash}"
                    if cache_key in cache:
                        cache[cache_key]['metadata'] = metadata
                        self.save_cache(cache)
                        print(f"  Updated metadata for podcast {podcast_id}")
                
                conn.close()
                
        except Exception as e:
            print(f"Error updating metadata for {podcast_id}: {e}")
    
    def parse_ttml_file(self, file_path):
        """Parse TTML file and extract transcript data"""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Extract podcast ID from filename
            filename = os.path.basename(file_path)
            # The filename format appears to be: transcript_XXXXX.ttml-XXXXX.ttml
            # We want to extract the ID (XXXXX part)
            
            # Try to extract ID between 'transcript_' and '.ttml'
            import re
            match = re.search(r'transcript_(\d+)', filename)
            if match:
                podcast_id = match.group(1)
            else:
                # Fallback: extract all digits
                podcast_id = ''.join(filter(str.isdigit, filename.replace('.ttml', '')))
            
            if not podcast_id:
                print(f"  Could not extract ID from filename: {filename}")
                return None
            
            print(f"  Extracted ID: {podcast_id} from filename: {filename}")
            
            # Parse XML - try without namespace first for simpler parsing
            try:
                root = ET.fromstring(content)
            except ET.ParseError as e:
                print(f"  XML Parse error: {e}")
                return None
            
            # Find all speaking chunks
            podcast_chunks = []
            
            # Register namespaces for cleaner iteration
            namespaces = {
                'tt': 'http://www.w3.org/ns/ttml',
                'ttm': 'http://www.w3.org/ns/ttml#metadata',
                'podcasts': 'http://podcasts.apple.com/transcript-ttml-internal'
            }
            
            # Find all p elements within body
            body = root.find('.//tt:body', namespaces)
            if not body:
                # Try without namespace
                body = root.find('.//body')
            
            if body:
                # Find all p elements
                p_elements = body.findall('.//tt:p', namespaces) or body.findall('.//p')
                
                for p in p_elements:
                    speaker = p.get('{http://www.w3.org/ns/ttml#metadata}agent') or 'Unknown'
                    
                    # Find sentence spans
                    sentence_spans = p.findall('.//tt:span[@podcasts:unit="sentence"]', namespaces)
                    if not sentence_spans:
                        # Try without namespace prefix
                        sentence_spans = [span for span in p.findall('.//span') 
                                        if span.get('{http://podcasts.apple.com/transcript-ttml-internal}unit') == 'sentence']
                    
                    # Create a chunk for each sentence (or group of sentences)
                    for sent_span in sentence_spans:
                        # Get all word spans within this sentence
                        word_spans = sent_span.findall('.//tt:span', namespaces) or sent_span.findall('.//span')
                        words = []
                        for word_span in word_spans:
                            if word_span.text:
                                words.append(word_span.text)
                        
                        sentence = ' '.join(words)
                        if sentence:
                            podcast_chunks.append({
                                'speaker': speaker,
                                'sentences': sentence
                            })
            
            return {
                'id': podcast_id,
                'chunks': podcast_chunks,
                'lastModified': os.path.getmtime(file_path) * 1000
            }
            
        except Exception as e:
            print(f"Error parsing {file_path}: {e}")
            return None
    
    def send_cached_podcasts(self):
        """Send pre-parsed podcast data using persistent cache with stored TTML files"""
        try:
            # Load existing cache
            cache = self.load_cache()
            cache_updated = False
            
            # Ensure TTML cache directory exists
            os.makedirs(self.TTML_CACHE_DIR, exist_ok=True)
            
            # Source directories
            podcast_base = os.path.expanduser('~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts')
            ttml_dir = os.path.join(podcast_base, 'Library/Cache/Assets/TTML')
            db_source_path = os.path.join(podcast_base, 'Documents/MTLibrary.sqlite')
            
            transcripts = {}
            podcast_ids_found = set()
            db_metadata = {}
            
            # First, check cached TTML files
            if os.path.exists(self.TTML_CACHE_DIR):
                print(f"Loading cached TTML files from {self.TTML_CACHE_DIR}")
                for filename in os.listdir(self.TTML_CACHE_DIR):
                    if filename.endswith('.ttml'):
                        cached_file_path = os.path.join(self.TTML_CACHE_DIR, filename)
                        file_hash = self.get_file_hash(cached_file_path)
                        cache_key = f"ttml_{file_hash}"
                        
                        if cache_key in cache:
                            print(f"  Using cached metadata for: {filename}")
                            cached_data = cache[cache_key]
                            # Store file path and metadata, but parse transcript on demand
                            transcripts[cached_data['id']] = {
                                'id': cached_data['id'],
                                'file_path': cached_file_path,
                                'lastModified': cached_data['lastModified'],
                                'file_hash': file_hash
                            }
                            podcast_ids_found.add(cached_data['id'])
                            
                            # If we have cached metadata, add it to db_metadata
                            if 'metadata' in cached_data:
                                db_metadata[cached_data['id']] = cached_data['metadata']
                        else:
                            # Re-parse cached file to get metadata
                            print(f"  Re-parsing cached file for metadata: {filename}")
                            try:
                                parsed = self.parse_ttml_file(cached_file_path)
                                if parsed and parsed['chunks']:
                                    # Store only metadata in cache, not transcript content
                                    metadata = {
                                        'id': parsed['id'],
                                        'lastModified': parsed['lastModified'],
                                        'chunk_count': len(parsed['chunks'])
                                    }
                                    cache[cache_key] = metadata
                                    cache_updated = True
                                    
                                    transcripts[parsed['id']] = {
                                        'id': parsed['id'],
                                        'file_path': cached_file_path,
                                        'lastModified': parsed['lastModified'],
                                        'file_hash': file_hash
                                    }
                                    podcast_ids_found.add(parsed['id'])
                                    print(f"    Successfully parsed: {parsed['id']} with {len(parsed['chunks'])} chunks")
                            except Exception as e:
                                print(f"    Error re-parsing {filename}: {e}")
            
            # Check for new TTML files in source directory
            if os.path.exists(ttml_dir):
                print(f"Checking for new TTML files in {ttml_dir}")
                
                for root, dirs, files in os.walk(ttml_dir):
                    for file in files:
                        if file.endswith('.ttml'):
                            source_path = os.path.join(root, file)
                            file_hash = self.get_file_hash(source_path)
                            
                            if not file_hash:
                                continue
                            
                            cache_key = f"ttml_{file_hash}"
                            
                            # Check if this file is already cached
                            if cache_key in cache:
                                cached_data = cache[cache_key]
                                if cached_data['id'] not in podcast_ids_found:
                                    print(f"  Using existing cache for: {file}")
                                    # Find the cached file
                                    cached_filename = f"{file_hash}_{file}"
                                    cached_file_path = os.path.join(self.TTML_CACHE_DIR, cached_filename)
                                    
                                    transcripts[cached_data['id']] = {
                                        'id': cached_data['id'],
                                        'file_path': cached_file_path,
                                        'lastModified': cached_data['lastModified'],
                                        'file_hash': file_hash
                                    }
                                    podcast_ids_found.add(cached_data['id'])
                                    
                                    # If we have cached metadata, add it to db_metadata
                                    if 'metadata' in cached_data:
                                        db_metadata[cached_data['id']] = cached_data['metadata']
                            else:
                                # New file - copy to cache and parse
                                print(f"  Processing new file: {file}")
                                try:
                                    file_size = os.path.getsize(source_path)
                                    if file_size > 1000000:  # 1MB limit
                                        print(f"    Skipping large file ({file_size} bytes)")
                                        continue
                                    
                                    # Copy file to cache directory
                                    cached_filename = f"{file_hash}_{file}"
                                    cached_file_path = os.path.join(self.TTML_CACHE_DIR, cached_filename)
                                    shutil.copy2(source_path, cached_file_path)
                                    print(f"    Copied to cache: {cached_filename}")
                                    
                                    # Parse the cached file to get metadata
                                    parsed = self.parse_ttml_file(cached_file_path)
                                    if parsed and parsed['chunks']:
                                        # Store only metadata in cache
                                        metadata = {
                                            'id': parsed['id'],
                                            'lastModified': parsed['lastModified'],
                                            'chunk_count': len(parsed['chunks'])
                                        }
                                        cache[cache_key] = metadata
                                        cache_updated = True
                                        
                                        transcripts[parsed['id']] = {
                                            'id': parsed['id'],
                                            'file_path': cached_file_path,
                                            'lastModified': parsed['lastModified'],
                                            'file_hash': file_hash
                                        }
                                        podcast_ids_found.add(parsed['id'])
                                        print(f"    Successfully processed: {parsed['id']} with {len(parsed['chunks'])} chunks")
                                
                                except Exception as e:
                                    print(f"    Error processing {file}: {e}")
            
            print(f"Total unique podcasts found: {len(podcast_ids_found)}")
            
            # Check database and get metadata only for found podcasts
            db_metadata = {}
            if os.path.exists(db_source_path) and podcast_ids_found:
                print(f"Reading database metadata for {len(podcast_ids_found)} podcasts")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    # Copy database to temp directory
                    db_path = os.path.join(temp_dir, 'MTLibrary.sqlite')
                    shutil.copy2(db_source_path, db_path)
                    
                    # Also copy WAL file if it exists
                    wal_source = db_source_path + '-wal'
                    if os.path.exists(wal_source):
                        shutil.copy2(wal_source, db_path + '-wal')
                    
                    try:
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()
                        
                        # Get column info
                        cursor.execute("PRAGMA table_info(ZMTEPISODE);")
                        columns = [row[1] for row in cursor.fetchall()]
                        
                        # Build query with podcast info join
                        select_fields = [
                            "e.ZSTORETRACKID", "e.ZAUTHOR", "e.ZCLEANEDTITLE", "e.ZITUNESSUBTITLE", "e.ZDURATION",
                            "p.ZTITLE as PODCAST_TITLE", "p.ZAUTHOR as PODCAST_AUTHOR", "p.ZWEBPAGEURL", 
                            "p.ZFEEDURL", "p.ZITEMDESCRIPTION as PODCAST_DESCRIPTION", "p.ZIMAGEURL", "p.ZCATEGORY"
                        ]
                        if "ZFIRSTTIMEAVAILABLE" in columns:
                            select_fields.append("e.ZFIRSTTIMEAVAILABLE")
                        
                        # Get metadata with podcast info for episodes we have transcripts for
                        podcast_ids_list = list(podcast_ids_found)
                        placeholders = ','.join(['?' for _ in podcast_ids_list])
                        query = f"""
                        SELECT {', '.join(select_fields)}
                        FROM ZMTEPISODE e
                        LEFT JOIN ZMTPODCAST p ON e.ZPODCAST = p.Z_PK
                        WHERE CAST(e.ZSTORETRACKID AS TEXT) IN ({placeholders})
                        """
                        cursor.execute(query, podcast_ids_list)
                        
                        found_in_db = 0
                        for row in cursor.fetchall():
                            podcast_id = str(row[0])
                            if podcast_id in podcast_ids_found:
                                metadata = {
                                    # Episode info
                                    'author': row[1] or 'Unknown',
                                    'title': row[2] or 'Unknown',
                                    'description': row[3] or '',
                                    'duration': row[4] or -1,
                                    'time': -1,
                                    # Podcast info
                                    'podcast_title': row[5] or 'Unknown',
                                    'podcast_author': row[6] or 'Unknown',
                                    'podcast_url': row[7] or '',
                                    'podcast_feed_url': row[8] or '',
                                    'podcast_description': row[9] or '',
                                    'podcast_image_url': row[10] or '',
                                    'podcast_category': row[11] or ''
                                }
                                
                                # Handle ZFIRSTTIMEAVAILABLE if present
                                time_index = 12 if len(row) > 12 else -1
                                if time_index > 0 and row[time_index]:
                                    metadata['time'] = row[time_index] + 978307200
                                
                                db_metadata[podcast_id] = metadata
                                found_in_db += 1
                        
                        conn.close()
                        print(f"Found metadata for {found_in_db} podcasts in database")
                        
                    except Exception as e:
                        print(f"Database error: {e}")
            
            # Add database metadata to cache
            if db_metadata:
                for podcast_id, metadata in db_metadata.items():
                    if podcast_id in transcripts:
                        # Add metadata to the transcript info in cache
                        file_hash = transcripts[podcast_id]['file_hash']
                        cache_key = f"ttml_{file_hash}"
                        if cache_key in cache:
                            cache[cache_key]['metadata'] = metadata
                            cache_updated = True
            
            # Save cache if updated
            if cache_updated:
                self.save_cache(cache)
            
            # Build response data grouped by podcast show
            shows = {}
            
            for pid, transcript_data in transcripts.items():
                # Try to get metadata from cache first, then from db_metadata, then defaults
                cached_metadata = None
                if 'file_hash' in transcript_data:
                    cache_key = f"ttml_{transcript_data['file_hash']}"
                    if cache_key in cache and 'metadata' in cache[cache_key]:
                        cached_metadata = cache[cache_key]['metadata']
                
                metadata = cached_metadata or db_metadata.get(pid, {
                    'author': 'Unknown',
                    'title': f'Podcast {pid}',
                    'description': '',
                    'duration': -1,
                    'time': -1,
                    'podcast_title': 'Unknown',
                    'podcast_author': 'Unknown',
                    'podcast_url': '',
                    'podcast_feed_url': '',
                    'podcast_description': '',
                    'podcast_image_url': '',
                    'podcast_category': ''
                })
                
                # Parse TTML file to get transcript content
                transcript_chunks = []
                try:
                    if os.path.exists(transcript_data['file_path']):
                        parsed = self.parse_ttml_file(transcript_data['file_path'])
                        if parsed and parsed['chunks']:
                            transcript_chunks = parsed['chunks']
                except Exception as e:
                    print(f"Error reading transcript for {pid}: {e}")
                
                # Use first few transcript chunks as description if needed
                if not metadata['description'] and transcript_chunks:
                    preview_chunks = []
                    char_count = 0
                    for chunk in transcript_chunks:
                        if char_count > 400:
                            break
                        preview_chunks.append(chunk['sentences'])
                        char_count += len(chunk['sentences'])
                    
                    preview = ' '.join(preview_chunks)
                    metadata['description'] = preview[:500] + '...' if len(preview) > 500 else preview
                
                # Group episodes by podcast show
                show_id = metadata['podcast_title']
                if show_id not in shows:
                    shows[show_id] = {
                        'id': show_id,
                        'title': metadata['podcast_title'],
                        'author': metadata['podcast_author'],
                        'url': metadata['podcast_url'],
                        'feed_url': metadata['podcast_feed_url'],
                        'description': metadata['podcast_description'],
                        'image_url': metadata['podcast_image_url'],
                        'category': metadata['podcast_category'],
                        'episodes': []
                    }
                
                # Add episode to the show
                shows[show_id]['episodes'].append({
                    'id': pid,
                    'author': metadata['author'],
                    'title': metadata['title'],
                    'description': metadata['description'],
                    'duration': metadata['duration'],
                    'time': metadata['time'],
                    'transcripts': transcript_chunks,
                    'lastModified': transcript_data['lastModified']
                })
            
            # Sort episodes within each show by publication time (or last modified if time unavailable)
            total_episodes = 0
            for show in shows.values():
                show['episodes'].sort(key=lambda x: x['time'] if x['time'] != -1 else x['lastModified'], reverse=True)
                total_episodes += len(show['episodes'])
            
            # Convert shows dict to list and sort by most recent episode
            shows_list = []
            for show in shows.values():
                if show['episodes']:
                    # Use publication time if available, otherwise use lastModified
                    latest_episode = show['episodes'][0]
                    show['latest_episode_time'] = latest_episode['time'] if latest_episode['time'] != -1 else latest_episode['lastModified']
                    shows_list.append(show)
            
            shows_list.sort(key=lambda x: x['latest_episode_time'], reverse=True)
            
            print(f"Total shows: {len(shows_list)}, Total episodes: {total_episodes}")
            
            # Send response
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            response = {
                'shows': shows_list,
                'total_shows': len(shows_list),
                'total_episodes': total_episodes,
                'cached_at': time.time(),
                'cache_entries': len(cache)
            }
            
            self.wfile.write(json.dumps(response).encode('utf-8'))
                
        except Exception as e:
            print(f"Error: {e}")
            self.send_error(500, str(e))
    
    def delete_episode(self):
        """Delete an episode from cache and stored TTML files"""
        try:
            # Read request body
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            episode_id = data.get('episode_id')
            if not episode_id:
                self.send_error(400, "Missing episode_id")
                return
            
            print(f"Attempting to delete episode: {episode_id}")
            
            # Load cache
            cache = self.load_cache()
            deleted = False
            deleted_file = None
            
            # Find and remove the episode from cache
            cache_keys_to_remove = []
            for cache_key, cached_data in cache.items():
                if cache_key.startswith('ttml_') and cached_data.get('id') == episode_id:
                    cache_keys_to_remove.append(cache_key)
                    
                    # Find the corresponding TTML file
                    file_hash = cache_key.replace('ttml_', '')
                    for filename in os.listdir(self.TTML_CACHE_DIR):
                        if filename.startswith(file_hash):
                            file_path = os.path.join(self.TTML_CACHE_DIR, filename)
                            try:
                                os.remove(file_path)
                                deleted_file = filename
                                print(f"Deleted TTML file: {filename}")
                            except Exception as e:
                                print(f"Error deleting TTML file {filename}: {e}")
                            break
                    deleted = True
            
            # Remove from cache
            for cache_key in cache_keys_to_remove:
                del cache[cache_key]
                print(f"Removed cache entry: {cache_key}")
            
            if deleted:
                # Save updated cache
                self.save_cache(cache)
                
                # Send success response
                response = {
                    'success': True,
                    'message': f'Episode {episode_id} deleted successfully',
                    'deleted_file': deleted_file
                }
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
                
                # Notify WebSocket clients
                def notify_clients():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(broadcast_update({
                            'type': 'podcast_updated',
                            'message': f'Episode "{episode_id}" deleted'
                        }))
                        loop.close()
                    except Exception as e:
                        print(f"WebSocket notification error: {e}")
                
                notification_thread = threading.Thread(target=notify_clients)
                notification_thread.daemon = True
                notification_thread.start()
                
            else:
                # Episode not found
                response = {
                    'success': False,
                    'message': f'Episode {episode_id} not found'
                }
                
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
                
        except Exception as e:
            print(f"Error deleting episode: {e}")
            response = {
                'success': False,
                'message': str(e)
            }
            
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))

class PodcastServer:
    def __init__(self):
        self.handler = PodcastRequestHandler
        
    def process_new_ttml_file(self, source_path):
        # Create a temporary handler instance for file processing
        temp_handler = type('TempHandler', (), {})()
        temp_handler.CACHE_FILE = PodcastRequestHandler.CACHE_FILE
        temp_handler.TTML_CACHE_DIR = PodcastRequestHandler.TTML_CACHE_DIR
        temp_handler.load_cache = PodcastRequestHandler.load_cache.__get__(temp_handler)
        temp_handler.save_cache = PodcastRequestHandler.save_cache.__get__(temp_handler)
        temp_handler.get_file_hash = PodcastRequestHandler.get_file_hash.__get__(temp_handler)
        temp_handler.parse_ttml_file = PodcastRequestHandler.parse_ttml_file.__get__(temp_handler)
        temp_handler.update_podcast_metadata = PodcastRequestHandler.update_podcast_metadata.__get__(temp_handler)
        temp_handler.process_new_ttml_file = PodcastRequestHandler.process_new_ttml_file.__get__(temp_handler)
        
        temp_handler.process_new_ttml_file(source_path)

def run_server(port=8000):
    # Create a server instance for the watcher
    server_address = ('', port)
    httpd = HTTPServer(server_address, PodcastRequestHandler)
    server_instance = PodcastServer()
    
    # Set up file monitoring
    podcast_base = os.path.expanduser('~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts')
    ttml_dir = os.path.join(podcast_base, 'Library/Cache/Assets/TTML')
    
    observer = None
    if os.path.exists(ttml_dir):
        print(f"Setting up file monitoring for: {ttml_dir}")
        event_handler = TTMLWatcher(server_instance)
        observer = Observer()
        observer.schedule(event_handler, ttml_dir, recursive=True)
        observer.start()
        print("File monitoring started")
    else:
        print(f"TTML directory not found: {ttml_dir}")
    
    # Start WebSocket server
    websocket_port = port + 1
    
    def start_websocket_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run_server():
            start_server = await websockets.serve(websocket_handler, "localhost", websocket_port)
            print(f"WebSocket server running on ws://localhost:{websocket_port}")
            await start_server.wait_closed()
        
        loop.run_until_complete(run_server())
    
    websocket_thread = threading.Thread(target=start_websocket_server)
    websocket_thread.daemon = True
    websocket_thread.start()
    
    print(f"Cached server running on http://localhost:{port}")
    print(f"WebSocket server running on ws://localhost:{websocket_port}")
    print("This version creates local copies to avoid SQLite conflicts")
    print("Automatically monitors for new TTML files with real-time UI updates")
    print("Processing podcast data from:")
    print("~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        if observer:
            observer.stop()
            observer.join()
        httpd.shutdown()

if __name__ == '__main__':
    run_server()