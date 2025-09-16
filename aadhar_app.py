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
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Configure upload settings
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Load environment variables
load_dotenv()

# ✅ Database connection
db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="",  
    database="aadhaar_db"
)
cursor = db.cursor()

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

def extract_aadhaar_data_locally(text):
    """Fallback function to extract Aadhaar data using regex (if Groq fails)"""
    print("🔧 Using local extraction as fallback...")
    
    # Clean the text
    text = re.sub(r'\s+', ' ', text)  # Remove extra whitespace
    
    # Extract patterns
    patterns = {
        'aadhaar_number': r'\b\d{4}\s?\d{4}\s?\d{4}\b',
        'date_of_birth': r'\b\d{2}/\d{2}/\d{4}\b',
        'gender': r'\b(MALE|FEMALE|Male|Female|M|F)\b'
    }
    
    extracted = {}
    
    # Extract using patterns
    for field, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            extracted[field] = match.group()
    
    # Simple name extraction (improve as needed)
    words = text.split()
    # Remove common Aadhaar card headers
    headers = ["GOVERNMENT", "INDIA", "AADHAAR", "UNIQUE", "IDENTIFICATION", "AUTHORITY", "OF"]
    clean_words = [word for word in words if word.upper() not in headers and word.isalpha() and len(word) > 2]
    
    # Take first 2-3 words as name
    name = " ".join(clean_words[:3]) if clean_words else ""
    
    # Simple address extraction (last few meaningful words)
    address_words = [word for word in words[-10:] if not re.match(r'\d{4}\s?\d{4}\s?\d{4}', word)]
    address = " ".join(address_words) if address_words else ""
    
    return {
        "name": name,
        "aadhaar_number": extracted.get('aadhaar_number', ''),
        "date_of_birth": extracted.get('date_of_birth', ''),
        "gender": extracted.get('gender', ''),
        "address": address
    }

@app.route('/')
def index():
    """Serve the main form page"""
    try:
        return render_template('aadhaar_form.html')
    except Exception as e:
        return f"Error loading template: {e}", 500

@app.route('/extract_aadhaar', methods=['POST'])
def extract_aadhaar():
    """Extract Aadhaar details from uploaded images"""
    try:
        print("📤 Received request to extract Aadhaar data")
        
        # Check if files were uploaded
        if 'aadhaar_images' not in request.files:
            return jsonify({"error": "No images uploaded"}), 400
        
        files = request.files.getlist('aadhaar_images')
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
                print("🤖 Using Groq LLM for data extraction...")
                
                prompt = f"""
You are an expert at extracting information from Indian Aadhaar cards.

IMPORTANT CONTEXT about Aadhaar cards:
- The cardholder's name appears prominently at the top
- Father's/Husband's name appears below with prefixes like "S/O", "D/O", "W/O", "Father:", "Husband:"
- The cardholder's name is usually in larger font and appears first
- Father's/Husband's name is secondary information

Text from Aadhaar card: {combined_text}

Extract the following information and return ONLY valid JSON:

Required fields:
- name: The CARDHOLDER's name (NOT father's/husband's name). If not present, return "Not Available".
- aadhaar_number: 12-digit number (format: XXXX XXXX XXXX). If not present, return "Not Available".
- date_of_birth: Date in DD/MM/YYYY format. If not present, return "Not Available".
- gender: Male/Female/Other. If not present, return "Not Available".

EXTRACTION RULES:
1. For NAME: Take the name that appears BEFORE any of these indicators: "S/O", "D/O", "W/O", "Father", "Husband", "Son of", "Daughter of", "Wife of"
2. Skip any text that contains government headers like "GOVERNMENT OF INDIA", "AADHAAR", "UNIQUE IDENTIFICATION"
3. Do NOT guess or make up any value. If the field is not clearly available, return "Not Available".
4. For ADDRESS: Extract it only if the keyword "Address" (or variations like "Addr", "Residence") is present in the text. Otherwise, return "Not Available".
5. Return only valid JSON in this format:
{{
  "name": "...",
  "aadhaar_number": "...",
  "date_of_birth": "...",
  "gender": "...",
  "address": "..."
}}
"""
                
                response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {
                            "role": "system", 
                            "content": "You are an expert at extracting structured data from Indian Aadhaar cards. Always return valid JSON only."
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
            extracted_data = extract_aadhaar_data_locally(combined_text)
        
        # ✅ Insert into Database
        try:
            sql = """INSERT INTO aadhaar_details (name, aadhaar_number, date_of_birth, gender, address) 
                     VALUES (%s, %s, %s, %s, %s)"""
            values = (
                extracted_data.get("name", ""),
                extracted_data.get("aadhaar_number", ""),
                extracted_data.get("date_of_birth", ""),
                extracted_data.get("gender", ""),
                extracted_data.get("address", "")
            )
            cursor.execute(sql, values)
            db.commit()
            print("✅ Data saved in database")
        except Exception as db_error:
            print(f"❌ Database Error: {db_error}")
        
        # Clean up uploaded files (optional)
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

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "groq_available": client is not None,
        "upload_folder": os.path.exists(UPLOAD_FOLDER)
    })

if __name__ == '__main__':
    print("🚀 Starting Aadhaar Form Automation Server...")
    print(f"📁 Upload folder: {UPLOAD_FOLDER}")
    print(f"🤖 Groq API: {'Available' if client else 'Not available'}")
    
    # Create templates folder if it doesn't exist
    templates_dir = os.path.join(app.root_path, 'templates')
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
        print(f"📂 Created templates directory: {templates_dir}")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
