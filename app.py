from flask import Flask, request, jsonify, render_template
import easyocr
import os
import json
import re
from groq import Groq
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import mysql.connector

app = Flask(__name__)

# Initialize OCR
reader = easyocr.Reader(['en'])

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'pdf'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Configure upload settings
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Load environment variables
load_dotenv()

# Database connection with error handling
try:
    db = mysql.connector.connect(
    host=os.getenv("SQL_SERVER"),   # e.g., "mydb.mysql.database.azure.com"
    user=os.getenv("SQL_USER"),     
    password=os.getenv("SQL_PASSWORD"),
    database=os.getenv("SQL_DB")
)
    cursor = db.cursor()
    print("✅ Database connected successfully")
    
    # Create table if not exists
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bank_details (
        id INT AUTO_INCREMENT PRIMARY KEY,
        bank_name VARCHAR(100),
        branch_name VARCHAR(100),
        ifsc_code VARCHAR(20),
        name VARCHAR(100),
        pan_no VARCHAR(20),
        cif VARCHAR(50),
        phone_number VARCHAR(15),
        account VARCHAR(50),
        nominee VARCHAR(100),
        address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    print("✅ Database table ready")
    
except mysql.connector.Error as err:
    print(f"❌ Database Error: {err}")
    db = None
    cursor = None

# Initialize Groq client
try:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    print("✅ Groq API client initialized")
except Exception as e:
    print(f"❌ Error initializing Groq client: {e}")
    client = None

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def preprocess_bank_text(text):
    """Preprocess bank document text for better extraction"""
    # Clean up common OCR artifacts
    text = re.sub(r'\s+', ' ', text)  # Multiple spaces to single space
    text = re.sub(r'[|]+', ' ', text)  # Remove pipe characters
    text = re.sub(r'_+', ' ', text)  # Remove underscores
    
    # Split into lines for analysis
    lines = []
    for line in text.split('\n'):
        line = line.strip()
        if line and len(line) > 2:
            lines.append(line)
    
    return lines, text

# --- Environment Variables ---
SQL_SERVER = os.getenv("SQL_SERVER")      
SQL_DB = os.getenv("SQL_DB")              
SQL_USER = os.getenv("SQL_USER")          
SQL_PASSWORD = os.getenv("SQL_PASSWORD")  
VECTORSTORE_PATH = os.getenv("VECTORSTORE_PATH", "chroma_store")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")

def extract_bank_data_locally(text):
    """Enhanced local extraction for bank documents"""
    print("🔧 Using enhanced local extraction for bank data...")
    
    lines, clean_text = preprocess_bank_text(text)
    
    print(f"📄 Processing {len(lines)} text lines...")
    
    # Enhanced patterns for bank documents
    patterns = {
        'ifsc_code': [
            r'\b[A-Z]{4}0[A-Z0-9]{6}\b',  # Standard IFSC format
            r'IFSC[:\s]*([A-Z]{4}0[A-Z0-9]{6})',
            r'IFS[:\s]*([A-Z]{4}0[A-Z0-9]{6})'
        ],
        'account_number': [
            r'\b\d{9,18}\b',  # Account numbers are typically 9-18 digits
            r'A/C[:\s]*(\d{9,18})',
            r'ACCOUNT[:\s]*(\d{9,18})',
            r'ACC[:\s]*(\d{9,18})'
        ],
        'pan_number': [
            r'\b[A-Z]{5}\d{4}[A-Z]\b',  # PAN format
            r'PAN[:\s]*([A-Z]{5}\d{4}[A-Z])'
        ],
        'phone_number': [
            r'\b[6-9]\d{9}\b',  # Indian mobile numbers
            r'MOBILE[:\s]*([6-9]\d{9})',
            r'PHONE[:\s]*([6-9]\d{9})',
            r'MOB[:\s]*([6-9]\d{9})'
        ],
        'cif': [
            r'CIF[:\s]*(\d{8,12})',
            r'CUSTOMER[:\s]*ID[:\s]*(\d{8,12})',
            r'ID[:\s]*(\d{8,12})'
        ]
    }
    
    extracted = {}
    
    # Extract using multiple patterns
    for field, pattern_list in patterns.items():
        for pattern in pattern_list:
            matches = re.findall(pattern, clean_text, re.IGNORECASE)
            if matches:
                # Take the first valid match
                match = matches[0] if isinstance(matches[0], str) else matches[0]
                extracted[field] = match
                break
    
    # Extract bank name (usually appears early in the document)
    bank_keywords = ['BANK', 'BANKING', 'FINANCIAL', 'COOPERATIVE', 'CREDIT', 'UNION']
    bank_name = ""
    
    for line in lines[:10]:  # Check first 10 lines
        line_upper = line.upper()
        if any(keyword in line_upper for keyword in bank_keywords):
            # Clean up the bank name
            bank_line = re.sub(r'[^A-Za-z\s&]', ' ', line)
            bank_line = re.sub(r'\s+', ' ', bank_line).strip()
            if len(bank_line) > 5:
                bank_name = bank_line
                break
    
    # Extract customer name (avoid bank names and headers)
    customer_name = ""
    skip_keywords = ['BANK', 'STATEMENT', 'ACCOUNT', 'PASSBOOK', 'BRANCH', 'ADDRESS', 'PHONE', 'MOBILE', 'IFSC', 'CODE']
    
    for line in lines:
        line_clean = re.sub(r'[^A-Za-z\s]', ' ', line)
        line_clean = re.sub(r'\s+', ' ', line_clean).strip()
        
        # Skip if contains skip keywords or numbers
        if (line_clean and 
            len(line_clean.split()) >= 2 and 
            len(line_clean.split()) <= 4 and
            not any(keyword in line_clean.upper() for keyword in skip_keywords) and
            not re.search(r'\d', line_clean) and
            len(line_clean) > 5):
            
            customer_name = line_clean
            break
    
    # Extract branch name
    branch_name = ""
    branch_keywords = ['BRANCH', 'BR.', 'OFFICE']
    for line in lines:
        line_upper = line.upper()
        if any(keyword in line_upper for keyword in branch_keywords):
            branch_line = re.sub(r'[^A-Za-z\s]', ' ', line)
            branch_line = re.sub(r'\s+', ' ', branch_line).strip()
            if 'BRANCH' in branch_line.upper() and len(branch_line) > 10:
                branch_name = branch_line
                break
    
    # Extract address (lines containing address keywords)
    address_lines = []
    address_keywords = ['ADDRESS', 'ADDR', 'RESIDENCE', 'PIN', 'PINCODE']
    
    for line in lines:
        line_upper = line.upper()
        if any(keyword in line_upper for keyword in address_keywords):
            # Clean address line
            addr_line = re.sub(r'ADDRESS[:\s]*', '', line, flags=re.IGNORECASE)
            addr_line = addr_line.strip()
            if addr_line and len(addr_line) > 5:
                address_lines.append(addr_line)
    
    address = " ".join(address_lines) if address_lines else ""
    
    # Extract nominee (if present)
    nominee = ""
    nominee_keywords = ['NOMINEE', 'NOMINY', 'BENEFICIARY']
    for line in lines:
        line_upper = line.upper()   
        if any(keyword in line_upper for keyword in nominee_keywords):
            nominee_line = re.sub(r'NOMINEE[:\s]*', '', line, flags=re.IGNORECASE)
            nominee_line = re.sub(r'[^A-Za-z\s]', ' ', nominee_line)
            nominee_line = re.sub(r'\s+', ' ', nominee_line).strip()
            if nominee_line and len(nominee_line) > 3:
                nominee = nominee_line
                break
    
    result = {
        "bank_name": bank_name or "Not Available",
        "branch_name": branch_name or "Not Available",
        "ifsc_code": extracted.get('ifsc_code', 'Not Available'),
        "name": customer_name or "Not Available",
        "pan_no": extracted.get('pan_number', 'Not Available'),
        "cif": extracted.get('cif', 'Not Available'),
        "phone_number": extracted.get('phone_number', 'Not Available'),
        "account": extracted.get('account_number', 'Not Available'),
        "nominee": nominee or "Not Available",
        "address": address or "Not Available"
    }
    
    print(f"📋 Local extraction result: {result}")
    return result

@app.route('/')
def index():
    """Serve the main bank form page"""
    try:
        return render_template('bank_form.html')
    except Exception as e:
        return f"Error loading template: {e}", 500

@app.route('/extract_bank', methods=['POST'])
def extract_bank():
    """Extract bank details from uploaded documents"""
    try:
        print("📤 Received request to extract bank data")
        
        # Check if files were uploaded
        if 'bank_images' not in request.files:
            return jsonify({"error": "No images uploaded"}), 400
        
        files = request.files.getlist('bank_images')
        if not files or all(file.filename == '' for file in files):
            return jsonify({"error": "No images selected"}), 400
        
        combined_text = ""
        processed_files = []
        
        # Process each uploaded image
        for image in files:
            if image and allowed_file(image.filename):
                # Secure filename
                filename = secure_filename(image.filename)
                image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                
                # Save image
                image.save(image_path)
                processed_files.append(image_path)
                
                print(f"📸 Processing image: {filename}")
                
                # Extract text using EasyOCR
                try:
                    results = reader.readtext(image_path)
                    text = " ".join([detection[1] for detection in results])
                    combined_text += " " + text
                    print(f"📄 Extracted text: {text[:100]}...")
                except Exception as ocr_error:
                    print(f"❌ OCR Error for {filename}: {ocr_error}")
                    continue
        
        if not combined_text.strip():
            return jsonify({"error": "No text could be extracted from images"}), 400
        
        print(f"🔤 Combined text length: {len(combined_text)} characters")
        
        # Try to use Groq LLM for extraction
        extracted_data = None
        
        if client:
            try:
                print("🤖 Using Groq LLM for bank data extraction...")
                
                prompt = f"""
You are an expert at extracting structured data from bank documents (passbooks, statements, account opening forms).

Extract the following information from this bank document text and return ONLY valid JSON:

Text: {combined_text}

Required fields:
- bank_name: Name of the bank (e.g., "State Bank of India", "HDFC Bank")
- branch_name: Branch name or location
- ifsc_code: 11-character IFSC code (format: ABCD0123456)
- name: Account holder's name (NOT bank staff names or branch names)
- pan_no: PAN number (format: ABCDE1234F)
- cif: Customer ID/CIF number
- phone_number: Mobile/phone number (10 digits)
- account: Bank account number
- nominee: Nominee name if mentioned
- address: Customer's address with full address
EXTRACTION RULES:
1. Bank name: Look for words like "BANK", "BANKING", "FINANCIAL SERVICES"
2. Account holder name: Look for customer name, avoid bank employee names
3. IFSC: Always 11 characters, starts with 4 letters, 5th character is 0
4. Account number: Usually 9-18 digits
5. PAN: Format ABCDE1234F (5 letters, 4 digits, 1 letter)
6. If any field is not found, return "Not Available"

Return only valid JSON:
{{
    "bank_name": "...",
    "branch_name": "...",
    "ifsc_code": "...",
    "name": "...",
    "pan_no": "...",
    "cif": "...",
    "phone_number": "...",
    "account": "...",
    "nominee": "...",
    "address": "..."
}}
"""
                
                response = client.chat.completions.create(
                    model= MODEL_NAME,
                    messages=[
                        {
                            "role": "system", 
                            "content": "You are an expert at extracting data from Indian bank documents. You understand bank passbooks, statements, and account forms. Always return valid JSON only."
                        },
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=500
                )
                
                # Get the response content
                llm_response = response.choices[0].message.content.strip()
                print(f"🤖 LLM Response: {llm_response}")
                
                # Try to parse JSON from LLM response
                try:
                    # Clean the response (remove any markdown formatting)
                    if '```json' in llm_response:
                        llm_response = llm_response.split('```json')[1].split('```')[0].strip()
                    elif '```' in llm_response:
                        llm_response = llm_response.split('```')[1].strip()
                    
                    extracted_data = json.loads(llm_response)
                    print("✅ Successfully parsed LLM response")
                    
                except json.JSONDecodeError as json_error:
                    print(f"❌ JSON parsing error: {json_error}")
                    print(f"Raw response: {llm_response}")
                    extracted_data = None
                    
            except Exception as groq_error:
                print(f"❌ Groq API Error: {groq_error}")
                extracted_data = None
        
        # Fallback to local extraction if Groq fails
        if not extracted_data:
            print("🔄 Falling back to local regex extraction...")
            extracted_data = extract_bank_data_locally(combined_text)
        
        # Validate and clean extracted data
        if extracted_data:
            # Clean up the data
            for key, value in extracted_data.items():
                if isinstance(value, str):
                    extracted_data[key] = value.strip()
                    # Replace empty strings with "Not Available"
                    if not extracted_data[key]:
                        extracted_data[key] = "Not Available"
        
        # Save to database
        if db and cursor and extracted_data:
            try:
                print("💾 Saving to database...")
                sql = """INSERT INTO bank_details 
                (bank_name, branch_name, ifsc_code, name, pan_no, cif, phone_number, account, nominee, address) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                
                values = (
                    extracted_data.get("bank_name", "Not Available"),
                    extracted_data.get("branch_name", "Not Available"),
                    extracted_data.get("ifsc_code", "Not Available"),
                    extracted_data.get("name", "Not Available"),
                    extracted_data.get("pan_no", "Not Available"),
                    extracted_data.get("cif", "Not Available"),
                    extracted_data.get("phone_number", "Not Available"),
                    extracted_data.get("account", "Not Available"),
                    extracted_data.get("nominee", "Not Available"),
                    extracted_data.get("address", "Not Available")
                )
                
                cursor.execute(sql, values)
                db.commit()
                print("✅ Data saved to database successfully")
                
            except mysql.connector.Error as db_error:
                print(f"❌ Database Error: {db_error}")
            except Exception as e:
                print(f"❌ General DB Error: {e}")
        
        # Clean up uploaded files
        for file_path in processed_files:
            try:
                os.remove(file_path)
            except:
                pass
        
        print(f"✅ Final extracted data: {extracted_data}")
        return jsonify(extracted_data)
        
    except Exception as e:
        print(f"❌ General Error: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/get_all_records', methods=['GET'])
def get_all_records():
    """Get all records from database"""
    try:
        if not db or not cursor:
            return jsonify({"error": "Database not available"}), 500
        
        cursor.execute("SELECT * FROM bank_details ORDER BY created_at DESC")
        records = cursor.fetchall()
        
        # Get column names
        cursor.execute("DESCRIBE bank_details")
        columns = [column[0] for column in cursor.fetchall()]
        
        # Convert to list of dictionaries
        result = []
        for record in records:
            record_dict = dict(zip(columns, record))
            # Convert datetime to string if present
            if 'created_at' in record_dict and record_dict['created_at']:
                record_dict['created_at'] = str(record_dict['created_at'])
            result.append(record_dict)
        
        return jsonify(result)
        
    except Exception as e:
        print(f"❌ Error fetching records: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/delete_record/<int:record_id>', methods=['DELETE'])
def delete_record(record_id):
    """Delete a specific record"""
    try:
        if not db or not cursor:
            return jsonify({"error": "Database not available"}), 500
        
        cursor.execute("DELETE FROM bank_details WHERE id = %s", (record_id,))
        db.commit()
        
        if cursor.rowcount > 0:
            return jsonify({"message": "Record deleted successfully"})
        else:
            return jsonify({"error": "Record not found"}), 404
            
    except Exception as e:
        print(f"❌ Error deleting record: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "groq_available": client is not None,
        "database_available": db is not None,
        "upload_folder": os.path.exists(UPLOAD_FOLDER)
    })

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum size is 16MB."}), 413

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    print("🚀 Starting Bank Form Automation Server...")
    print(f"📁 Upload folder: {UPLOAD_FOLDER}")
    print(f"🤖 Groq API: {'Available' if client else 'Not available'}")
    print(f"🗄️ Database: {'Connected' if db else 'Not connected'}")
    
    # Create templates folder if it doesn't exist
    templates_dir = os.path.join(app.root_path, 'templates')
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
        print(f"📂 Created templates directory: {templates_dir}")
    port = int(os.environ.get("PORT", 5000))   # Azure injects PORT dynamically
    app.run(debug=False, host='0.0.0.0', port=port)
