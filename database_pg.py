import os
import re
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
from contextlib import contextmanager
import random

# =====================
# CONNECTION POOL SETUP
# =====================

DATABASE_URL = os.environ.get('DATABASE_URL')

# Fix Railway's postgres:// to postgresql://
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

print(f"📊 PostgreSQL configured: {DATABASE_URL[:30]}..." if DATABASE_URL else "❌ No DATABASE_URL")

# Create connection pool (reuse connections for better performance)
connection_pool = None

def initialize_pool():
    """Initialize PostgreSQL connection pool."""
    global connection_pool
    
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable not set")
    
    try:
        connection_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor
        )
        print("✅ PostgreSQL connection pool initialized")
        return True
    except Exception as e:
        print(f"❌ Failed to initialize connection pool: {e}")
        return False


@contextmanager
def get_db_connection():
    """Context manager for database connections from pool."""
    if connection_pool is None:
        initialize_pool()
    
    conn = connection_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        connection_pool.putconn(conn)


# =====================
# AUTHENTICATION FUNCTIONS
# =====================

def is_user_authorized(phone_number):
    """Check if a phone number is authorized AND not expired."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT authorized, expiry_date 
            FROM authorized_users 
            WHERE phone_number = %s
        ''', (phone_number,))
        
        result = cursor.fetchone()
        
        if not result:
            return False
        
        authorized = result['authorized']
        expiry_date = result['expiry_date']
        
        if not authorized:
            return False
        
        if expiry_date and datetime.now() > expiry_date:
            return False
        
        return True


def is_admin(phone_number):
    """Check if a phone number is an admin."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id FROM admin_users WHERE phone_number = %s
        ''', (phone_number,))
        return cursor.fetchone() is not None


def log_auth_attempt(phone_number, action, success=False):
    """Log authentication attempts for security."""
    if not phone_number:
        phone_number = "UNKNOWN_SENDER"
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO auth_logs (phone_number, action, success)
            VALUES (%s, %s, %s)
        ''', (phone_number, action, success))


# =====================
# ADMIN FUNCTIONS
# =====================

def add_user(phone_number, name=None, expiry_days=None):
    """Add a new authorized user."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            expiry_date = None
            if expiry_days:
                expiry_date = datetime.now() + timedelta(days=expiry_days)
            
            cursor.execute('''
                INSERT INTO authorized_users (phone_number, name, expiry_date)
                VALUES (%s, %s, %s)
            ''', (phone_number, name, expiry_date))
            
            # Auto-enable tips
            cursor.execute('''
                INSERT INTO user_tip_preferences (phone_number, receive_tips)
                VALUES (%s, true)
                ON CONFLICT (phone_number) DO NOTHING
            ''', (phone_number,))
            
            return True, "User added successfully!"
    except psycopg2.IntegrityError:
        return False, "User already exists in database"
    except Exception as e:
        return False, f"Error: {str(e)}"


def remove_user(phone_number):
    """Remove/deactivate a user."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE authorized_users 
            SET authorized = false
            WHERE phone_number = %s
        ''', (phone_number,))
        
        if cursor.rowcount > 0:
            return True, "User deactivated successfully!"
        else:
            return False, "User not found"


def reactivate_user(phone_number):
    """Reactivate a previously deactivated user."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE authorized_users 
            SET authorized = true
            WHERE phone_number = %s
        ''', (phone_number,))
        
        if cursor.rowcount > 0:
            return True, "User reactivated successfully!"
        else:
            return False, "User not found"


def list_all_users():
    """Get list of all users."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT phone_number, name, authorized, date_added, expiry_date
            FROM authorized_users
            ORDER BY date_added DESC
        ''')
        return cursor.fetchall()


def get_user_info(phone_number):
    """Get detailed info about a specific user."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM authorized_users WHERE phone_number = %s
        ''', (phone_number,))
        return cursor.fetchone()


# =====================
# UTILITY FUNCTIONS
# =====================

def get_total_users():
    """Get total number of authorized users."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM authorized_users WHERE authorized = true')
        return cursor.fetchone()['count']


def clean_expired_users():
    """Deactivate users whose subscription has expired."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE authorized_users 
            SET authorized = false
            WHERE expiry_date IS NOT NULL 
            AND expiry_date < NOW()
            AND authorized = true
        ''')
        count = cursor.rowcount
        if count > 0:
            print(f"🧹 Cleaned {count} expired users")
        return count


# =====================
# MENTAL HEALTH TIPS FUNCTIONS
# =====================

def add_mental_health_tip(tip_text, category='general'):
    """Add a new mental health tip."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO mental_health_tips (tip_text, category)
                VALUES (%s, %s)
                RETURNING id
            ''', (tip_text, category))
            tip_id = cursor.fetchone()['id']
            return True, "Tip added successfully!", tip_id
    except Exception as e:
        return False, f"Error: {str(e)}", None


def get_all_mental_health_tips(active_only=True):
    """Get all mental health tips."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if active_only:
            cursor.execute('''
                SELECT * FROM mental_health_tips 
                WHERE active = true
                ORDER BY category, id
            ''')
        else:
            cursor.execute('''
                SELECT * FROM mental_health_tips 
                ORDER BY category, id
            ''')
        return cursor.fetchall()


def get_tip_by_id(tip_id):
    """Get a specific tip by ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM mental_health_tips WHERE id = %s
        ''', (tip_id,))
        return cursor.fetchone()


def deactivate_tip(tip_id):
    """Deactivate a mental health tip."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE mental_health_tips 
            SET active = false
            WHERE id = %s
        ''', (tip_id,))
        
        if cursor.rowcount > 0:
            return True, "Tip deactivated successfully!"
        else:
            return False, "Tip not found"


def activate_tip(tip_id):
    """Reactivate a mental health tip."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE mental_health_tips 
            SET active = true
            WHERE id = %s
        ''', (tip_id,))
        
        if cursor.rowcount > 0:
            return True, "Tip reactivated successfully!"
        else:
            return False, "Tip not found"


def get_next_tip_for_user(phone_number):
    """Get the next tip for a user using smart rotation."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get all active tips
        cursor.execute('SELECT id FROM mental_health_tips WHERE active = true')
        all_tips = [row['id'] for row in cursor.fetchall()]
        
        if not all_tips:
            return None
        
        # Get recent tips
        cursor.execute('''
            SELECT DISTINCT tip_id 
            FROM user_tip_history 
            WHERE phone_number = %s
            AND sent_date >= CURRENT_DATE - INTERVAL '15 days'
        ''', (phone_number,))
        
        recent_tips = [row['tip_id'] for row in cursor.fetchall()]
        
        available_tips = [tip_id for tip_id in all_tips if tip_id not in recent_tips]
        
        if not available_tips:
            available_tips = all_tips
        
        selected_tip_id = random.choice(available_tips)
        
        cursor.execute('''
            SELECT * FROM mental_health_tips WHERE id = %s
        ''', (selected_tip_id,))
        
        return cursor.fetchone()


def log_tip_sent(phone_number, tip_id):
    """Log that a tip was sent to a user."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO user_tip_history (phone_number, tip_id, sent_date)
                VALUES (%s, %s, CURRENT_DATE)
            ''', (phone_number, tip_id))
            return True
    except Exception as e:
        print(f"Error logging tip: {e}")
        return False


# =====================
# USER TIP PREFERENCES
# =====================

def set_user_tip_preference(phone_number, receive_tips=True):
    """Set user's preference for receiving daily tips."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO user_tip_preferences (phone_number, receive_tips)
                VALUES (%s, %s)
                ON CONFLICT(phone_number) DO UPDATE SET
                    receive_tips = EXCLUDED.receive_tips,
                    last_modified = NOW()
            ''', (phone_number, receive_tips))
            return True
    except Exception as e:
        print(f"Error setting tip preference: {e}")
        return False


def get_user_tip_preference(phone_number):
    """Get user's tip preferences."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM user_tip_preferences WHERE phone_number = %s
        ''', (phone_number,))
        result = cursor.fetchone()
        
        if not result:
            set_user_tip_preference(phone_number, True)
            return {'receive_tips': True, 'preferred_time': '07:00'}
        
        return result


def get_users_for_daily_tips():
    """Get all users who should receive daily tips."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT au.phone_number, au.name
            FROM authorized_users au
            LEFT JOIN user_tip_preferences utp ON au.phone_number = utp.phone_number
            WHERE au.authorized = true
            AND (utp.receive_tips IS NULL OR utp.receive_tips = true)
        ''')
        return cursor.fetchall()


# =====================
# TIP STATISTICS
# =====================

def get_user_tip_stats(phone_number):
    """Get statistics about tips sent to a user."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT COUNT(*) as total FROM user_tip_history 
            WHERE phone_number = %s
        ''', (phone_number,))
        total = cursor.fetchone()['total']
        
        cursor.execute('''
            SELECT COUNT(*) as recent FROM user_tip_history 
            WHERE phone_number = %s
            AND sent_date >= CURRENT_DATE - INTERVAL '30 days'
        ''', (phone_number,))
        recent = cursor.fetchone()['recent']
        
        cursor.execute('''
            SELECT MAX(sent_date) as last_date FROM user_tip_history 
            WHERE phone_number = %s
        ''', (phone_number,))
        last_date = cursor.fetchone()['last_date']
        
        return {
            'total_tips_received': total,
            'tips_last_30_days': recent,
            'last_tip_date': str(last_date) if last_date else None
        }


def get_global_tip_stats():
    """Get global statistics about tips."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM mental_health_tips WHERE active = true')
        total_tips = cursor.fetchone()['count']
        
        cursor.execute('''
            SELECT COUNT(*) as count FROM user_tip_history 
            WHERE sent_date = CURRENT_DATE
        ''')
        tips_today = cursor.fetchone()['count']
        
        cursor.execute('''
            SELECT COUNT(*) as count FROM user_tip_preferences 
            WHERE receive_tips = true
        ''')
        users_enabled = cursor.fetchone()['count']
        
        cursor.execute('''
            SELECT category, COUNT(*) as count 
            FROM mental_health_tips 
            WHERE active = true
            GROUP BY category
        ''')
        categories = {row['category']: row['count'] for row in cursor.fetchall()}
        
        return {
            'total_active_tips': total_tips,
            'tips_sent_today': tips_today,
            'users_with_tips_enabled': users_enabled,
            'tips_by_category': categories
        }


# =====================
# WORKOUT TRACKING
# =====================

def log_workout_completion(phone_number, workout_minutes, calories_burned, progress_percent, goal):
    """Save workout data for progress tracking."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO workout_logs (phone_number, workout_minutes, calories_burned, progress_percent, goal)
                VALUES (%s, %s, %s, %s, %s)
            ''', (phone_number, workout_minutes, calories_burned, progress_percent, goal))
            return True
    except Exception as e:
        print(f"Error logging workout: {e}")
        return False


def get_weekly_progress(phone_number):
    """Get user's workout stats for the last 7 days."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                COUNT(*) as workouts_completed,
                SUM(workout_minutes) as total_minutes,
                SUM(calories_burned) as total_calories,
                AVG(progress_percent) as avg_progress,
                goal
            FROM workout_logs
            WHERE phone_number = %s
            AND date_completed >= NOW() - INTERVAL '7 days'
            GROUP BY goal
        ''', (phone_number,))
        
        result = cursor.fetchone()
        
        if result and result['workouts_completed'] > 0:
            return dict(result)
        return None


def get_users_for_weekly_report():
    """Get all active users for sending weekly reports."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT phone_number, name 
            FROM authorized_users 
            WHERE authorized = true
        ''')
        return cursor.fetchall()


# =====================
# BONUS TIPS (Same logic as SQLite)
# =====================

def get_personalized_bonus_tips(user_data):
    """Returns 1-2 highly relevant bonus tips based on user's profile."""
    # Same implementation as SQLite version
    tips = []
    gender = str(user_data.get("gender", "")).strip().lower()
    goal = str(user_data.get("fitness_goal", "")).strip().lower()
    injury = str(user_data.get("injury", "")).strip().lower()

    if gender in ["female", "woman", "f", "girl"]:
        tips.append("As a woman, your energy & strength fluctuate with your menstrual cycle. "
                    "Train heavy during follicular phase (Day 1–14), go lighter during luteal phase. "
                    "Listen to your body — it's smart!")

    if "knee" in injury or "acl" in injury:
        tips.append("Knee injury? Replace squats/jumps with Spanish squats, reverse sled drags, "
                    "or step-ups. Build quads without stressing the joint!")

    if "weight loss" in goal or "fat loss" in goal:
        tips.append("*Pro tip:* Walk 8–10k steps daily + strength training 3x/week "
                    "burns MORE fat than cardio alone. Muscle = 24/7 calorie burner!")

    if "muscle" in goal or "bulk" in goal:
        tips.append("Want to gain muscle fast? Eat in surplus + sleep 8+ hrs + "
                    "train each muscle 2x/week. Progressive overload is king!")

    return tips[:2]


# =====================
# STREAK TRACKING
# =====================

def initialize_streak_tracking():
    """Initialize streak tracking table."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS workout_streaks (
                    id SERIAL PRIMARY KEY,
                    phone_number TEXT UNIQUE NOT NULL,
                    current_streak INTEGER DEFAULT 0,
                    longest_streak INTEGER DEFAULT 0,
                    last_workout_date DATE,
                    FOREIGN KEY (phone_number) REFERENCES authorized_users(phone_number)
                )
            ''')
            print("✅ Streak tracking table initialized!")
            return True
    except Exception as e:
        print(f"Error initializing streak tracking: {e}")
        return False


def update_workout_streak(phone_number):
    """Update user's workout streak."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        today = date.today()
        
        cursor.execute('''
            SELECT current_streak, longest_streak, last_workout_date 
            FROM workout_streaks 
            WHERE phone_number = %s
        ''', (phone_number,))
        
        result = cursor.fetchone()
        
        if not result:
            cursor.execute('''
                INSERT INTO workout_streaks (phone_number, current_streak, longest_streak, last_workout_date)
                VALUES (%s, 1, 1, %s)
            ''', (phone_number, today))
            return (1, True, False)
        
        current_streak = result['current_streak']
        longest_streak = result['longest_streak']
        last_date = result['last_workout_date']
        
        if last_date == today:
            return (current_streak, False, False)
        
        if last_date == today - timedelta(days=1):
            current_streak += 1
            broke_streak = False
        else:
            current_streak = 1
            broke_streak = True
        
        is_new_record = current_streak > longest_streak
        if is_new_record:
            longest_streak = current_streak
        
        cursor.execute('''
            UPDATE workout_streaks 
            SET current_streak = %s, 
                longest_streak = %s, 
                last_workout_date = %s
            WHERE phone_number = %s
        ''', (current_streak, longest_streak, today, phone_number))
        
        return (current_streak, is_new_record, broke_streak)


def get_user_streak(phone_number):
    """Get user's current streak information."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT current_streak, longest_streak, last_workout_date
            FROM workout_streaks
            WHERE phone_number = %s
        ''', (phone_number,))
        
        result = cursor.fetchone()
        
        if not result:
            return {
                'current_streak': 0,
                'longest_streak': 0,
                'last_workout_date': None
            }
        
        return dict(result)


def get_streak_leaderboard(limit=10):
    """Get top users by current streak."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                ws.phone_number,
                au.name,
                ws.current_streak,
                ws.longest_streak
            FROM workout_streaks ws
            JOIN authorized_users au ON ws.phone_number = au.phone_number
            WHERE au.authorized = true
            ORDER BY ws.current_streak DESC, ws.longest_streak DESC
            LIMIT %s
        ''', (limit,))
        
        return cursor.fetchall()


# =====================
# USER PROFILES
# =====================

def save_user_profile(phone_number, profile_data):
    """Save or update complete user profile."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO user_profiles (
                    phone_number, name, age, gender,
                    weight, height, bmi, fitness_goal,
                    medical_conditions, injuries, allergies,
                    diet_preference, activity_level, stress_level,
                    workout_duration, workout_location, workout_time, exercises_to_avoid,
                    profile_completed
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(phone_number) DO UPDATE SET
                    name = EXCLUDED.name,
                    age = EXCLUDED.age,
                    gender = EXCLUDED.gender,
                    weight = EXCLUDED.weight,
                    height = EXCLUDED.height,
                    bmi = EXCLUDED.bmi,
                    fitness_goal = EXCLUDED.fitness_goal,
                    medical_conditions = EXCLUDED.medical_conditions,
                    injuries = EXCLUDED.injuries,
                    allergies = EXCLUDED.allergies,
                    diet_preference = EXCLUDED.diet_preference,
                    activity_level = EXCLUDED.activity_level,
                    stress_level = EXCLUDED.stress_level,
                    workout_duration = EXCLUDED.workout_duration,
                    workout_location = EXCLUDED.workout_location,
                    workout_time = EXCLUDED.workout_time,
                    exercises_to_avoid = EXCLUDED.exercises_to_avoid,
                    profile_completed = EXCLUDED.profile_completed,
                    last_updated = NOW()
            ''', (
                phone_number,
                profile_data.get('name'),
                profile_data.get('age'),
                profile_data.get('gender'),
                profile_data.get('weight'),
                profile_data.get('height'),
                profile_data.get('bmi'),
                profile_data.get('fitness_goal'),
                profile_data.get('medical_conditions'),
                profile_data.get('injuries'),
                profile_data.get('allergies'),
                profile_data.get('diet_preference'),
                profile_data.get('activity_level'),
                profile_data.get('stress_level'),
                profile_data.get('workout_duration'),
                profile_data.get('workout_location'),
                profile_data.get('workout_time'),
                profile_data.get('exercises_to_avoid'),
                profile_data.get('profile_completed', False)
            ))
            
            return True
    except Exception as e:
        print(f"Error saving user profile: {e}")
        return False


def get_user_profile(phone_number):
    """Get user's complete profile."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM user_profiles WHERE phone_number = %s
        ''', (phone_number,))
        
        result = cursor.fetchone()
        return dict(result) if result else None


def update_profile_field(phone_number, field_name, field_value):
    """Update a specific profile field."""
    allowed_fields = [
        'name', 'age', 'gender', 'weight', 'height', 'bmi', 'fitness_goal',
        'medical_conditions', 'injuries', 'allergies',
        'diet_preference', 'activity_level', 'stress_level',
        'workout_duration', 'workout_location', 'workout_time', 'exercises_to_avoid', 'profile_completed'
    ]
    
    if field_name not in allowed_fields:
        return False
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            query = f'''
                UPDATE user_profiles 
                SET {field_name} = %s, last_updated = NOW()
                WHERE phone_number = %s
            '''
            cursor.execute(query, (field_value, phone_number))
            return cursor.rowcount > 0
    except Exception as e:
        print(f"Error updating profile field: {e}")
        return False


def mark_profile_completed(phone_number):
    """Mark user's profile as completed."""
    return update_profile_field(phone_number, 'profile_completed', True)


def is_profile_completed(phone_number):
    """Check if user has completed their profile."""
    profile = get_user_profile(phone_number)
    if not profile:
        return False
    return profile.get('profile_completed', False)


def get_profile_completion_percentage(phone_number):
    """Calculate profile completion percentage."""
    profile = get_user_profile(phone_number)
    
    if not profile:
        return 0
    
    required_fields = [
        'name', 'age', 'gender',
        'weight', 'height', 'fitness_goal',
        'diet_preference', 'activity_level',
        'workout_duration', 'workout_location'
    ]
    
    filled_fields = sum(1 for field in required_fields if profile.get(field))
    return int((filled_fields / len(required_fields)) * 100)


def calculate_bmi(weight_str, height_str):
    """Calculate BMI from weight and height strings."""
    # Same implementation as SQLite version
    try:
        weight = None
        weight_str_lower = str(weight_str).lower().strip()
        
        weight_match = re.search(r'(\d+\.?\d*)', weight_str_lower)
        if weight_match:
            weight = float(weight_match.group(1))
        
        if 'lb' in weight_str_lower or 'lbs' in weight_str_lower:
            weight = weight * 0.453592
        
        if weight < 30 or weight > 300:
            return None
        
        height_cm = None
        height_str_lower = str(height_str).lower().strip()
        
        feet_pattern = r"(\d+)\s*['\-\s]+(\d+)?(?:\s*['\"]|(?:ft|feet)?)"
        feet_match = re.search(feet_pattern, height_str_lower)
        
        if feet_match:
            feet = int(feet_match.group(1))
            inches = int(feet_match.group(2)) if feet_match.group(2) else 0
            if 3 <= feet <= 8:
                height_cm = (feet * 30.48) + (inches * 2.54)
        
        if not height_cm:
            num_match = re.search(r'(\d+\.?\d*)', height_str_lower)
            if num_match:
                value = float(num_match.group(1))
                if value > 100:
                    height_cm = value
                elif value >= 3 and value < 10:
                    height_cm = value * 30.48
                else:
                    height_cm = value
        
        if not height_cm or height_cm < 100 or height_cm > 250:
            return None
        
        height_m = height_cm / 100
        bmi = weight / (height_m ** 2)
        return round(bmi, 1)
        
    except Exception as e:
        print(f"BMI calculation error: {e}")
        return None


def get_bmi_category(bmi):
    """Get BMI category."""
    if bmi is None:
        return "Unknown"
    
    try:
        bmi = float(bmi)
        if bmi < 18.5:
            return "Underweight"
        elif 18.5 <= bmi < 25:
            return "Healthy Weight"
        elif 25 <= bmi < 30:
            return "Overweight"
        else:
            return "Obese"
    except:
        return "Unknown"


def format_profile_summary(profile):
    """Format user profile into readable summary."""
    # Same implementation as SQLite version
    if not profile:
        return "No profile found"
    
    summary = f"📋 *Profile Summary*\n\n"
    summary += f"👤 *BASIC INFO*\n"
    summary += f"Name: {profile.get('name', 'N/A')}\n"
    summary += f"Age: {profile.get('age', 'N/A')}\n"
    summary += f"Gender: {profile.get('gender', 'N/A')}\n\n"
    
    summary += f"📊 *BODY METRICS*\n"
    summary += f"Weight: {profile.get('weight', 'N/A')}\n"
    summary += f"Height: {profile.get('height', 'N/A')}\n"
    
    bmi = profile.get('bmi')
    if bmi:
        category = get_bmi_category(bmi)
        summary += f"BMI: {bmi} ({category})\n"
    
    summary += f"Goal: {profile.get('fitness_goal', 'N/A')}\n\n"
    
    return summary


# =====================
# WORKOUT SCHEDULES
# =====================

def initialize_workout_schedule_table():
    """Create table for daily workout schedules."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_workout_schedule (
                    id SERIAL PRIMARY KEY,
                    phone_number TEXT UNIQUE NOT NULL,
                    preferred_time TEXT NOT NULL,
                    timezone TEXT DEFAULT 'Asia/Kolkata',
                    job_id TEXT,
                    active BOOLEAN DEFAULT true,
                    last_plan_sent TIMESTAMP,
                    date_created TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY (phone_number) REFERENCES authorized_users(phone_number)
                )
            ''')
            print("✅ Workout schedule table initialized!")
            return True
    except Exception as e:
        print(f"Error initializing workout schedule table: {e}")
        return False


def normalize_workout_time(time_str):
    """Convert various time formats to HH:MM."""
    # Same implementation as SQLite version
    if not time_str:
        return "07:00"
    
    time_str = str(time_str).lower().strip()
    
    time_pattern = r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?'
    match = re.search(time_pattern, time_str)
    
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        meridiem = match.group(3)
        
        if meridiem:
            if meridiem == 'pm' and hour != 12:
                hour += 12
            elif meridiem == 'am' and hour == 12:
                hour = 0
        
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    
    preset_times = {
        'early morning': '05:00',
        'morning': '07:00',
        'afternoon': '14:00',
        'evening': '18:00',
        'night': '20:00',
        'flexible': '07:00'
    }
    
    for key, value in preset_times.items():
        if key in time_str:
            return value
    
    return "07:00"


def save_workout_schedule(phone_number, preferred_time):
    """Save user's preferred workout time."""
    try:
        normalized_time = normalize_workout_time(preferred_time)
        
        if not normalized_time:
            return False, None
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO daily_workout_schedule (phone_number, preferred_time)
                VALUES (%s, %s)
                ON CONFLICT(phone_number) DO UPDATE SET
                    preferred_time = EXCLUDED.preferred_time,
                    active = true
            ''', (phone_number, normalized_time))
        
        return True, normalized_time
        
    except Exception as e:
        print(f"Error saving workout schedule: {e}")
        return False, None


def get_all_scheduled_users():
    """Get all users with active workout schedules."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                dws.phone_number,
                dws.preferred_time,
                dws.job_id,
                au.name
            FROM daily_workout_schedule dws
            JOIN authorized_users au ON dws.phone_number = au.phone_number
            WHERE dws.active = true AND au.authorized = true
        ''')
        return cursor.fetchall()


def update_schedule_job_id(phone_number, job_id):
    """Update the APScheduler job ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE daily_workout_schedule
            SET job_id = %s
            WHERE phone_number = %s
        ''', (job_id, phone_number))


def mark_plan_sent(phone_number):
    """Mark that a workout plan was sent."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE daily_workout_schedule
            SET last_plan_sent = NOW()
            WHERE phone_number = %s
        ''', (phone_number,))


def deactivate_workout_schedule(phone_number):
    """Deactivate daily workout plans."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE daily_workout_schedule
            SET active = false
            WHERE phone_number = %s
        ''', (phone_number,))
        
        if cursor.rowcount > 0:
            return True, "Daily workouts deactivated"
        return False, "User not found in schedule"


def get_user_workout_schedule(phone_number):
    """Get a user's workout schedule."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM daily_workout_schedule
            WHERE phone_number = %s
        ''', (phone_number,))
        return cursor.fetchone()


def get_all_user_profiles(limit=50):
    """Get all user profiles for admin view."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                up.*,
                au.authorized,
                au.expiry_date
            FROM user_profiles up
            JOIN authorized_users au ON up.phone_number = au.phone_number
            ORDER BY up.last_updated DESC
            LIMIT %s
        ''', (limit,))
        
        return cursor.fetchall()


def search_profiles_by_goal(fitness_goal):
    """Search profiles by fitness goal."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM user_profiles
            WHERE fitness_goal ILIKE %s
            ORDER BY last_updated DESC
        ''', (f'%{fitness_goal}%',))
        
        return cursor.fetchall()


# Initialize pool on module import
try:
    initialize_pool()
except Exception as e:
    print(f"Warning: Could not initialize pool on import: {e}")
