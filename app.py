import os
import re
import threading
import sys
import json

from datetime import datetime, timedelta, timezone
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
# from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, AnyMessage, SystemMessage
from langchain_core.messages import HumanMessage as LangChainHumanMessage
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify  # Add jsonify
import atexit 

# Import database functions
from database_pg import (
    is_user_authorized, is_admin, log_auth_attempt,
    add_user, remove_user, reactivate_user, list_all_users,
    get_user_info, clean_expired_users,
    # Mental health tips functions
    add_mental_health_tip, get_all_mental_health_tips, deactivate_tip, activate_tip,
    get_next_tip_for_user, log_tip_sent, set_user_tip_preference, 
    get_user_tip_preference, get_users_for_daily_tips, get_user_tip_stats,
    get_global_tip_stats, get_tip_by_id,
    # Workout tracking functions
    log_workout_completion, get_weekly_progress, get_users_for_weekly_report,
    get_personalized_bonus_tips,
    # Streak tracking functions
    initialize_streak_tracking, update_workout_streak, get_user_streak,
    # Enhanced profile functions - ADD THESE
    save_user_profile, get_user_profile, update_profile_field,
    mark_profile_completed, is_profile_completed,
    calculate_bmi, get_bmi_category, format_profile_summary,
    get_profile_completion_percentage,
    # Daily workout schedule functions
    save_workout_schedule, normalize_workout_time, 
    get_all_scheduled_users, update_schedule_job_id,
    mark_plan_sent, deactivate_workout_schedule,
    get_user_workout_schedule, initialize_workout_schedule_table, bootstrap_database
)

# -------------------------
# Twilio credentials
# -------------------------
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "admin@nexifit.com")

WELLNESS_TIP_TEMPLATE_SID = os.environ.get("WELLNESS_TIP_TEMPLATE_SID", "HXf4c280d2f9ce9387e54914cc5ef14e94")
MOTIVATIONAL_MESSAGE_TEMPLATE_SID = os.environ.get("MOTIVATIONAL_MESSAGE_TEMPLATE_SID", "HX797390caf87f46e260df8fdb97b19d48")

# Railway environment configuration
PORT = int(os.environ.get('PORT', 5000))

client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)

# -------------------------
# Initialize Gemini
# -------------------------
# GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
# llm = init_chat_model("gemini-2.5-flash", model_provider="google_genai")


from dotenv import load_dotenv
load_dotenv()
from langchain_groq import ChatGroq
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
llm = ChatGroq(api_key=GROQ_API_KEY, model="llama-3.3-70b-versatile")
# -------------------------
# LangGraph State
# -------------------------
class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

graph_builder = StateGraph(State)

def chatbot(state: State):
    response_message = llm.invoke(state["messages"])
    return {"messages": [response_message]}

graph_builder.add_node("chatbot", chatbot)
graph_builder.add_edge(START, "chatbot")
graph_builder.add_edge("chatbot", END)
graph = graph_builder.compile()

# -------------------------
# Flask App
# -------------------------
app = Flask(__name__)
user_sessions = {}

# ==========================================
# SCHEDULER CONFIGURATION - FIXED VERSION
# ==========================================

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

scheduler = None  # Global variable declaration

def init_scheduler():
    """Initialize scheduler - called at module load time."""
    global scheduler

    if scheduler is not None:
        print("⚠️ Scheduler already initialized")
        return scheduler

    print("\n" + "=" * 70)
    print("⏰ Initializing Scheduler...")
    print(f"📅 Current Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    scheduler = BackgroundScheduler(
        jobstores={'default': MemoryJobStore()},
        executors={'default': ThreadPoolExecutor(max_workers=3)},
        job_defaults={
            'coalesce': True,
            'max_instances': 1,
            'misfire_grace_time': 300
        },
        timezone='UTC'
    )

    try:
        # Start scheduler
        scheduler.start()
        print(f"✅ Scheduler started! Running: {scheduler.running}")

        # --------------------------------------------------
        # CORE SCHEDULED JOBS
        # --------------------------------------------------

        # Daily mental health tips (7:00 AM IST)
        scheduler.add_job(
            func=send_daily_mental_health_tips,
            trigger='cron',
            hour=1,
            minute=30,
            id='daily_mental_health_tips',
            name='Send Daily Mental Health Tips',
            replace_existing=True
        )
        print("✅ Daily tips scheduled: 7:00 AM IST (1:30 AM UTC)")

        # Weekly progress reports (Sunday 8:00 PM IST)
        scheduler.add_job(
            func=send_weekly_progress_reports,
            trigger='cron',
            day_of_week='sun',
            hour=14,
            minute=30,
            id='weekly_progress_reports',
            name='Weekly Progress Reports',
            replace_existing=True
        )
        print("✅ Weekly reports scheduled: Sundays 8:00 PM IST (2:30 PM UTC)")

        # Cleanup expired users (Daily 2:00 AM IST)
        scheduler.add_job(
            func=clean_expired_users,
            trigger='cron',
            hour=20,
            minute=30,
            id='clean_expired_users',
            name='Clean Expired Users',
            replace_existing=True
        )
        print("✅ Cleanup scheduled: Daily 2:00 AM IST (8:30 PM UTC)")

        # --------------------------------------------------
        # DAILY WORKOUT SCHEDULING (SAFE INIT)
        # --------------------------------------------------

        print("🛠 Bootstrapping database (Railway-safe)...")
        bootstrap_database()

        print("🛠 Initializing workout schedule table...")
        initialize_workout_schedule_table()

        print("📅 Restoring daily workout schedules...")
        try:
            reschedule_all_daily_workouts()
        except Exception as e:
            # Non-critical: never crash app on startup
            print("⚠️ Workout reschedule skipped:", e)

        # --------------------------------------------------
        # PRINT JOB SUMMARY
        # --------------------------------------------------

        jobs = scheduler.get_jobs()
        print(f"\n📋 Scheduled Jobs ({len(jobs)}):")
        for job in jobs:
            print(f"   - {job.name}")
            if job.next_run_time:
                print(f"     Next run: {job.next_run_time}")

        print("=" * 70 + "\n")

    except Exception as e:
        print(f"❌ Scheduler initialization failed: {e}")
        import traceback
        traceback.print_exc()

    return scheduler


# Graceful shutdown
def shutdown_scheduler():
    global scheduler
    if scheduler is not None and hasattr(scheduler, 'running') and scheduler.running:
        print("🛑 Shutting down scheduler...")
        try:
            scheduler.shutdown(wait=False)
            print("✅ Scheduler shut down")
        except Exception as e:
            print(f"⚠️ Error shutting down scheduler: {e}")

import atexit
atexit.register(shutdown_scheduler)
        
def send_message_via_twilio(phone_number, message):
    """Send message via Twilio - for scheduled jobs."""
    try:
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=phone_number,
            body=message
        )
        print(f"✅ Scheduled message sent to {phone_number}")
    except Exception as e:
        print(f"❌ Error sending message: {e}")
    
def schedule_motivational_message(sender, session, workout_minutes, calories_burned, progress_percent):
    """
    Schedule motivational message after workout using Twilio template.
    Falls back to regular message if template unavailable.
    """
    global scheduler
    
    if not scheduler or not scheduler.running:
        print("⚠️ Scheduler not running, cannot schedule motivational message")
        return
    
    try:
        name = session.get('name', 'Champion')
        goal = session.get('fitness_goal', 'your fitness goals')
        
        # Build streak message
        streak_message = ""
        streak_info = session.get('latest_streak')
        
        if streak_info:
            current = streak_info['current']
            
            if streak_info['is_record']:
                streak_message = f"🏆 *NEW RECORD!* {current} day streak! You're unstoppable! 🚀"
            elif current >= 7:
                streak_message = f"🔥 *{current} days in a row!* You're on fire! 💪"
            elif current >= 3:
                streak_message = f"✨ *{current} days streak!* Keep the momentum! 💪"
            else:
                streak_message = f"💪 Day {current} done! Every day counts!"
            
            if streak_info['broke']:
                streak_message += "\n\n🌱 New streak started! Let's build it up again!"
        else:
            streak_message = "💪 Great workout today!"
        
        # Calculate run time (send message after workout duration)
        run_time = datetime.now() + timedelta(minutes=workout_minutes)
        job_id = f"motivational_{sender}_{int(datetime.now().timestamp())}"
        
        # ✅ METHOD 1: Using Template (RECOMMENDED)
        def send_motivational_template():
            """Send motivational message using template or fallback."""
            try:
                if MOTIVATIONAL_MESSAGE_TEMPLATE_SID:
                    # Send using template
                    message = client.messages.create(
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=sender,
                        content_sid=MOTIVATIONAL_MESSAGE_TEMPLATE_SID,
                        content_variables=json.dumps({
                            "1": name,
                            "2": str(calories_burned or 0),
                            "3": str(progress_percent or 0),
                            "4": goal,
                            "5": streak_message
                        })
                    )
                    print(f"✅ Motivational template sent to {sender} (SID: {message.sid})")
                    
                else:
                    # Fallback to regular message
                    motivational_msg = (
                        f"🔥 Great job, {name}!\n\n"
                        f"Today you burned approximately {calories_burned or 0} calories "
                        f"and you're about {progress_percent or 0}% closer to your goal: *{goal}*.\n\n"
                        f"{streak_message}\n\n"
                        f"Keep it up! 💪"
                    )
                    
                    client.messages.create(
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=sender,
                        body=motivational_msg
                    )
                    print(f"✅ Motivational message sent to {sender}")
                    
            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg or "daily messages limit" in error_msg.lower():
                    print(f"⚠️ Rate limited - motivational message not sent to {sender}")
                else:
                    print(f"❌ Error sending motivational message to {sender}: {e}")
        
        # Schedule the job
        scheduler.add_job(
            func=send_motivational_template,
            trigger='date',
            run_date=run_time,
            id=job_id,
            name=f'Motivational message for {sender}',
            replace_existing=True
        )
        
        print(f"✅ Motivational message scheduled for {sender} at {run_time.strftime('%H:%M:%S')}")
        
    except Exception as e:
        print(f"❌ Error scheduling motivational message: {e}")
        import traceback
        traceback.print_exc()
        
# -------------------------
# System Prompt (Updated for conversational responses)
# -------------------------
fitness_system_prompt = SystemMessage(
    content=(
        "You are NexiFit, a helpful and conversational fitness assistant. "
        "You help users with workouts, nutrition, diet plans, and fitness advice.\n\n"
        "**CRITICAL: ALWAYS RESPECT USER CONSTRAINTS**\n"
        "1. NEVER exceed the user's specified workout duration\n"
        "2. ONLY suggest exercises for their specified location (Home = bodyweight/minimal equipment)\n"
        "3. NEVER include avoided exercises or similar movements\n"
        "4. STRICTLY avoid foods the user is allergic to\n"
        "5. Modify exercises for reported injuries\n"
        "6. Consider medical conditions in all recommendations\n\n"
        "**FORMATTING RULES - VERY IMPORTANT:**\n"
        "1. Use CLEAN TEXT - No asterisks, no markdown\n"
        "2. Use CAPITAL LETTERS for section headers\n"
        "3. Use simple dashes (-) for lists\n"
        "4. Use line separator: ━━━━━━━━━━━━━━━━━\n"
        "5. Use 3-space indentation for sub-items\n"
        "6. Generous line breaks between sections\n\n"
        
        "**FORMAT EXAMPLE:**\n\n"
        "TODAY'S WORKOUT PLAN\n"
        "Duration: 40 minutes\n"
        "Estimated Time: ~40 minutes\n\n" 
        
        "WARM-UP (5 minutes)\n\n"
        "   - Cat-Cow Stretch: 10 reps\n"
        "   - Arm Circles: 15 forward, 15 backward\n\n"
        
        "MAIN WORKOUT (30 minutes)\n\n"
        "   - Push-ups: 3 sets x 10 reps\n"
        "   - Squats: 3 sets x 15 reps\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        "NUTRITION PLAN\n\n"
        "Daily Targets:\n"
        "   - Protein: 150g\n"
        "   - Calories: 2500 kcal\n\n"
        
        "**Response Guidelines:**\n"
        "1. When providing an INITIAL workout plan, use this structured format:\n\n"
        "   TODAY'S WORKOUT PLAN\n"
        "   Duration: X minutes\n"
        "   Estimated Time: ~X minutes  ← CRITICAL: ALWAYS INCLUDE THIS LINE\n\n"  
        "   - Exercise 1: details\n"
        "   - Exercise 2: details\n\n"
        "   NUTRITION PLAN (Macros & Nutritional Guidelines)\n"
        "   - Daily protein target: Xg\n"
        "   - Daily calorie target: X calories\n"
        "   - Carbs/Fats ratio: ...\n"
        "   - Hydration: X liters water\n\n"
        "   DIET PLAN (Actual Meal Suggestions)\n"
        "   - Breakfast: ...\n"
        "   - Lunch: ...\n"
        "   - Dinner: ...\n"
        "   - Snacks: ...\n\n"
        "   RECOVERY\n"
        "   - Sleep: X hours\n"
        "   - Rest days: ...\n"
        "   - Stretching: ...\n\n"
        "2. MANDATORY: For EVERY initial workout plan, ALWAYS include the line:\n"
        "   'Estimated Time: ~X minutes' right after the Duration line\n"
        "   This is REQUIRED for tracking and motivational messages.\n\n"
        "3. NUTRITION vs DIET:\n"
        "   - Nutrition Plan = Numbers (calories, protein, carbs, fats, hydration)\n"
        "   - Diet Plan = Actual food/meals (breakfast, lunch, dinner)\n\n"
        "4. For FOLLOW-UP questions or conversations:\n"
        "   - Be natural and conversational\n"
        "   - Answer questions directly and clearly\n"
        "   - Keep responses concise (2-4 paragraphs max)\n"
        "   - Use bullet points only when listing multiple items\n"
        "   - Be encouraging and supportive\n\n"
        "5. Always stay on fitness topics: workouts, diet, nutrition, exercise, health, recovery, etc.\n"
        "6. If asked about workout modifications, alternatives, or specific exercises, answer directly.\n"
        "7. Keep tone friendly, motivating, and professional.\n"
        "8. Avoid emojis unless the user uses them first."
    )
)

# -------------------------
# MENTAL HEALTH TIPS FUNCTIONS
# -------------------------

def send_daily_mental_health_tips():
    """
    Send mental health tips to all eligible users every morning at 7 AM.
    Uses Twilio Content Templates for better deliverability.
    """
    print(f"\n{'='*50}")
    print(f"🌅 Starting daily mental health tips broadcast - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    
    # Get all users who should receive tips
    users = get_users_for_daily_tips()
    
    if not users:
        print("⚠️ No users found to send tips to")
        return
    
    success_count = 0
    error_count = 0
    rate_limited_count = 0
    
    # Category emoji mapping
    category_emoji = {
        'motivation': '💪',
        'stress': '🧘',
        'mindfulness': '🧠',
        'sleep': '😴',
        'positivity': '✨',
        'general': '💭'
    }
    
    for user in users:
        try:
            phone_number = user['phone_number']
            name = user['name'] or "there"
            
            # Get next tip for this user
            tip = get_next_tip_for_user(phone_number)
            
            if not tip:
                print(f"⚠️ No tips available for {phone_number}")
                error_count += 1
                continue
            
            emoji = category_emoji.get(tip['category'], '💭')
            
            # ✅ METHOD 1: Using Twilio Content Template (RECOMMENDED)
            if WELLNESS_TIP_TEMPLATE_SID and WELLNESS_TIP_TEMPLATE_SID != "HXxxxxx":
                try:
                    # Send using template
                    message = client.messages.create(
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=phone_number,
                        content_sid=WELLNESS_TIP_TEMPLATE_SID,
                        content_variables=json.dumps({
                            "1": name,           # {{1}} = name
                            "2": emoji,          # {{2}} = emoji
                            "3": tip['tip_text'] # {{3}} = tip text
                        })
                    )
                    
                    print(f"✅ Template message sent to {phone_number} (SID: {message.sid})")
                    
                except Exception as template_error:
                    # If template fails, fall back to regular message
                    print(f"⚠️ Template failed for {phone_number}, using fallback: {template_error}")
                    
                    message_body = (
                        f"🌅 Good morning, {name}!\n\n"
                        f"{emoji} *Today's Mental Wellness Tip:*\n\n"
                        f"{tip['tip_text']}\n\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"Remember: A healthy mind supports a healthy body! 💪🧠\n\n"
                        f"_Reply 'STOP TIPS' to unsubscribe from daily tips._"
                    )
                    
                    client.messages.create(
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=phone_number,
                        body=message_body
                    )
                    print(f"✅ Fallback message sent to {phone_number}")
            
            else:
                # ✅ METHOD 2: Regular message (if template not configured)
                message_body = (
                    f"🌅 Good morning, {name}!\n\n"
                    f"{emoji} *Today's Mental Wellness Tip:*\n\n"
                    f"{tip['tip_text']}\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Remember: A healthy mind supports a healthy body! 💪🧠\n\n"
                    f"_Reply 'STOP TIPS' to unsubscribe from daily tips._"
                )
                
                client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=phone_number,
                    body=message_body
                )
                print(f"✅ Regular message sent to {phone_number}")
            
            # Log the tip
            log_tip_sent(phone_number, tip['id'])
            success_count += 1
            
        except Exception as e:
            error_msg = str(e)
            
            # Check if rate limited
            if "429" in error_msg or "daily messages limit" in error_msg.lower():
                print(f"⚠️ Rate limited for {phone_number}")
                rate_limited_count += 1
            else:
                print(f"❌ Error sending tip to {phone_number}: {e}")
                error_count += 1
    
    print(f"\n{'='*50}")
    print(f"📊 Daily Tips Summary:")
    print(f"   ✅ Successful: {success_count}")
    print(f"   ❌ Failed: {error_count}")
    if rate_limited_count > 0:
        print(f"   ⚠️ Rate Limited: {rate_limited_count}")
    print(f"   📱 Total Users: {len(users)}")
    print(f"{'='*50}\n")

def send_weekly_progress_reports():
    """Send weekly progress reports to all users every Sunday."""
    print(f"\n{'='*50}")
    print(f"📊 Sending Weekly Progress Reports - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")
    
    users = get_users_for_weekly_report()
    success_count = 0
    
    for user in users:
        try:
            phone_number = user['phone_number']
            name = user['name'] or "Champion"
            
            # Get user's weekly progress
            progress = get_weekly_progress(phone_number)
            
            if not progress:
                # User hasn't worked out this week
                message = (
                    f"📊 *Weekly Progress Report*\n\n"
                    f"Hey {name}! 👋\n\n"
                    f"We noticed you haven't logged any workouts this week.\n\n"
                    f"💪 Even a 15-minute workout counts!\n"
                    f"Let's get back on track. Ready? 🚀"
                )
            else:
                # User has workout data
                workouts = progress['workouts_completed']
                minutes = int(progress['total_minutes'])
                calories = int(progress['total_calories'])
                progress_pct = round(progress['avg_progress'], 1)
                goal = progress['goal']
                
                # Format time
                hours = minutes // 60
                remaining_mins = minutes % 60
                time_str = f"{hours}h {remaining_mins}m" if hours > 0 else f"{remaining_mins} min"
                
                # Choose emoji based on performance
                if workouts >= 5:
                    emoji = "🔥"
                    praise = "Outstanding"
                elif workouts >= 3:
                    emoji = "💪"
                    praise = "Great job"
                else:
                    emoji = "👍"
                    praise = "Good start"
                
                message = (
                    f"📊 *Your Weekly Progress Report*\n\n"
                    f"{emoji} *{praise}, {name}!*\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📅 *This Week's Stats:*\n\n"
                    f"✅ Workouts: *{workouts}*\n"
                    f"⏱️ Time: *{time_str}*\n"
                    f"🔥 Calories: *~{calories} kcal*\n"
                    f"📈 Progress: *{progress_pct}%* closer\n\n"
                    f"🎯 *Goal:* {goal}\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Keep the momentum! 🚀"
                )

                # Streak report add on
                streak_data = get_user_streak(phone_number)
                if streak_data['current_streak'] > 0:
                    streak_emoji = "🔥" if streak_data['current_streak'] >= 7 else "💪"
                    message += f"{streak_emoji} *Current Streak:* {streak_data['current_streak']} days\n\n"
                
                message += "━━━━━━━━━━━━━━━━\nKeep the momentum! 🚀"
            
            # Send message
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=phone_number,
                body=message
            )
            
            print(f"✅ Sent report to {phone_number}")
            success_count += 1
            
        except Exception as e:
            print(f"❌ Error sending to {phone_number}: {e}")
    
    print(f"📊 Sent {success_count} reports\n{'='*50}\n")

def send_daily_workout_plan(phone_number):
    """
    Send automated daily workout plan to a user.
    Called by APScheduler at user's preferred time.
    
    Args:
        phone_number: User's WhatsApp number
    """
    from database_pg import mark_plan_sent, get_user_profile
    
    print(f"\n{'='*50}")
    print(f"💪 Sending daily workout to {phone_number}")
    print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    
    try:
        # Get user session or create minimal one from database
        if phone_number not in user_sessions:
            # Load profile from database
            profile = get_user_profile(phone_number)
            
            if not profile:
                print(f"⚠️ No profile found for {phone_number}")
                return
            
            # Create session from profile
            user_sessions[phone_number] = {
                "messages": [],
                "onboarding_step": "done",
                "profile_completed": True,
                "profile_confirmed": True,
                "name": profile['name'],
                "age": profile['age'],
                "gender": profile['gender'],
                "weight": profile['weight'],
                "height": profile['height'],
                "fitness_goal": profile['fitness_goal'],
                "medical_conditions": profile.get('medical_conditions'),
                "injuries": profile.get('injuries'),
                "allergies": profile.get('allergies'),
                "diet_preference": profile.get('diet_preference'),
                "activity_level": profile.get('activity_level'),
                "stress_level": profile.get('stress_level'),
                "workout_duration": profile.get('workout_duration'),
                "workout_location": profile.get('workout_location'),
                "workout_time": profile.get('workout_time'),
                "exercises_to_avoid": profile.get('exercises_to_avoid'),
                "just_viewed_profile": False,
                "reminders": [],
                "last_goal_check": datetime.now()
            }
        
        session = user_sessions[phone_number]
        
        # Build context message
        context_msg = f"Create today's personalized workout plan based on my profile."
        
        # Prepare system + context
        system_context = SystemMessage(
            content=(
                f"User's details:\n"
                f"- Name: {session['name']}\n"
                f"- Age: {session['age']}\n"
                f"- Gender: {session['gender']}\n"
                f"- Weight: {session['weight']}\n"
                f"- Height: {session['height']}\n"
                f"- Goal: {session['fitness_goal']}\n\n"
                
                f"🚨 STRICT CONSTRAINTS - DO NOT VIOLATE:\n"
                f"- Medical: {session.get('medical_conditions', 'None')} "
                f"{'(adjust recommendations accordingly)' if session.get('medical_conditions') != 'None' else ''}\n"
                f"- Injuries: {session.get('injuries', 'None')} "
                f"{'(provide modified exercises, avoid aggravating movements)' if session.get('injuries') != 'None' else ''}\n"
                f"- Allergies: {session.get('allergies', 'None')} "
                f"{'(NEVER include these foods in diet plan)' if session.get('allergies') != 'None' else ''}\n"
                f"- Workout Duration: {session.get('workout_duration', 'Not specified')} "
                f"(NEVER exceed this time)\n"
                f"- Location: {session.get('workout_location', 'Not specified')} "
                f"(Home = bodyweight/resistance bands/dumbbells only, NO barbells/machines)\n"
                f"- Exercises to Avoid: {session.get('exercises_to_avoid', 'None')} "
                f"(NEVER include these or similar movements)\n"
                f"- Diet Preference: {session.get('diet_preference', 'Not specified')}\n"
                f"- Activity Level: {session.get('activity_level', 'Not specified')}\n"
                f"- Stress Level: {session.get('stress_level', 'Not specified')}\n\n"
                
                f"Create a complete TODAY'S workout plan with:\n"
                f"- Clearly labeled as 'Today's Workout Plan' at top\n"
                f"- Warm-up, main workout, cool-down sections\n"
                f"- Nutrition Plan (macros: protein, calories, carbs, fats, water)\n"
                f"- Diet Plan (actual meals: breakfast, lunch, dinner, snacks)\n"
                f"- Recovery tips\n"
                f"- Adjust based on user's constraints and restrictions\n"
            )
        )
        
        state = {
            "messages": [
                fitness_system_prompt,
                system_context,
                HumanMessage(content=context_msg)
            ]
        }
        
        result = graph.invoke(state)
        response_text = result["messages"][-1].content.strip()
        
        # Clean formatting
        response_text = clean_response_formatting(response_text)
        
        # Add personalized bonus tips
        bonus_tips = get_personalized_bonus_tips(session)
        if bonus_tips:
            response_text += format_bonus_tips(bonus_tips)
        
        # Add motivational greeting header
        hour = datetime.now().hour
        if hour < 12:
            greeting = f"🌅 Good morning, {session['name']}!\n\n"
        elif hour < 17:
            greeting = f"☀️ Good afternoon, {session['name']}!\n\n"
        else:
            greeting = f"🌆 Good evening, {session['name']}!\n\n"
        
        response_text = greeting + response_text
        
        # Send via Twilio (with smart chunking)
        chunks = smart_chunk(response_text, 1500)
        
        for idx, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                body = f"(Part {idx}/{len(chunks)})\n\n{chunk}"
            else:
                body = chunk
            
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=phone_number,
                body=body
            )
            
            print(f"   ✅ Sent part {idx}/{len(chunks)}")
        
        # Mark as sent in database
        mark_plan_sent(phone_number)
        
        print(f"✅ Daily workout sent successfully to {phone_number}")
        print(f"{'='*50}\n")
        
    except Exception as e:
        print(f"❌ Error sending daily workout to {phone_number}: {e}")
        import traceback
        traceback.print_exc()


def schedule_user_daily_workout(phone_number, preferred_time):
    """
    Schedule daily workout plan for a user at their preferred time.
    
    Args:
        phone_number: User's WhatsApp number
        preferred_time: Time in HH:MM format (24-hour, IST)
        
    Returns:
        bool: Success status
    """
    global scheduler
    
    if not scheduler or not scheduler.running:
        print("⚠️ Scheduler not running, cannot schedule daily workout!")
        return False
    
    try:
        from database_pg import update_schedule_job_id
        
        # Parse time
        hour, minute = map(int, preferred_time.split(':'))
        
        # Validate
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            print(f"⚠️ Invalid time: {preferred_time}")
            return False
        
        # Create unique job ID
        job_id = f"daily_workout_{phone_number.replace(':', '_')}"
        
        # Remove existing job if any
        try:
            scheduler.remove_job(job_id)
            print(f"   ℹ️ Removed existing job: {job_id}")
        except:
            pass
        
        # Schedule daily job (IST timezone)
        scheduler.add_job(
            func=send_daily_workout_plan,
            trigger='cron',
            hour=hour,
            minute=minute,
            args=[phone_number],
            id=job_id,
            name=f'Daily Workout: {phone_number}',
            replace_existing=True,
            timezone='Asia/Kolkata',  # IST timezone
            misfire_grace_time=300  # 5 minutes grace period
        )
        
        # Save job ID to database
        update_schedule_job_id(phone_number, job_id)
        
        print(f"✅ Scheduled daily workout for {phone_number}")
        print(f"   Time: {preferred_time} IST ({hour:02d}:{minute:02d})")
        print(f"   Job ID: {job_id}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error scheduling daily workout: {e}")
        import traceback
        traceback.print_exc()
        return False


def reschedule_all_daily_workouts():
    """
    Reschedule all active users' daily workouts.
    Call this during app startup to restore schedules after restart.
    """
    from database_pg import get_all_scheduled_users
    
    print("\n" + "="*70)
    print("📅 Rescheduling all daily workouts on startup...")
    print("="*70)
    
    try:
        users = get_all_scheduled_users()
        
        if not users:
            print("ℹ️ No users with active workout schedules")
            print("="*70 + "\n")
            return
        
        success_count = 0
        failed_count = 0
        
        for user in users:
            phone_number = user['phone_number']
            preferred_time = user['preferred_time']
            name = user.get('name', 'Unknown')
            
            print(f"\n   Scheduling for {name} ({phone_number}):")
            
            if schedule_user_daily_workout(phone_number, preferred_time):
                success_count += 1
            else:
                failed_count += 1
        
        print(f"\n{'='*70}")
        print(f"📊 Rescheduling Summary:")
        print(f"   ✅ Success: {success_count}")
        print(f"   ❌ Failed: {failed_count}")
        print(f"   📱 Total: {len(users)}")
        print(f"{'='*70}\n")
        
    except Exception as e:
        print(f"❌ Error in reschedule_all_daily_workouts: {e}")
        import traceback
        traceback.print_exc()
        print("="*70 + "\n")

def handle_tip_admin_commands(sender, incoming_msg):
    """
    Handle admin commands for mental health tips management.
    Returns response message or None if not a tip command.
    """
    
    msg = incoming_msg.strip()
    msg_upper = msg.upper()
    
    # TIP HELP
    if msg_upper == "ADMIN TIP_HELP":
        return (
            "💭 *Mental Health Tips Commands:*\n\n"
            "ADMIN ADD_TIP category: text\n"
            "ADMIN LIST_TIPS [category]\n"
            "ADMIN VIEW_TIP <id>\n"
            "ADMIN REMOVE_TIP <id>\n"
            "ADMIN ACTIVATE_TIP <id>\n"
            "ADMIN TIP_STATS [phone]\n"
            "ADMIN TEST_TIP <phone>\n"
            "ADMIN BROADCAST_TIP\n\n"
            "Categories: motivation, stress, mindfulness, sleep, positivity, general\n\n"
            "Example:\n"
            "ADMIN ADD_TIP motivation: You are stronger than you think!"
        )
    
    # ADD TIP: ADMIN ADD_TIP category: text
    if msg_upper.startswith("ADMIN ADD_TIP"):
        try:
            # Parse: ADMIN ADD_TIP motivation: Your tip text here
            content = msg[14:].strip()  # Remove "ADMIN ADD_TIP "
            
            if ':' in content:
                category, tip_text = content.split(':', 1)
                category = category.strip().lower()
                tip_text = tip_text.strip()
            else:
                category = 'general'
                tip_text = content
            
            if len(tip_text) < 10:
                return "⚠️ Tip text too short. Minimum 10 characters."
            
            success, message, tip_id = add_mental_health_tip(tip_text, category)
            
            if success:
                return f"✅ Tip added successfully!\nID: {tip_id}\nCategory: {category}\nPreview: {tip_text[:100]}..."
            else:
                return f"⚠️ {message}"
                
        except Exception as e:
            return f"⚠️ Error: {str(e)}\n\nUsage:\nADMIN ADD_TIP category: tip text\nExample:\nADMIN ADD_TIP motivation: You are stronger than you think!"
    
    # LIST TIPS: ADMIN LIST_TIPS [category]
    elif msg_upper.startswith("ADMIN LIST_TIPS"):
        parts = incoming_msg.split()
        category_filter = parts[2].lower() if len(parts) > 2 else None
        
        tips = get_all_mental_health_tips(active_only=True)
        
        if category_filter:
            tips = [tip for tip in tips if tip['category'] == category_filter]
        
        if not tips:
            return f"📋 No tips found{' for category: ' + category_filter if category_filter else ''}"
        
        # Group by category
        categories = {}
        for tip in tips:
            cat = tip['category']
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(tip)
        
        response = f"📋 *Mental Health Tips* ({len(tips)} total)\n\n"
        
        for cat, cat_tips in sorted(categories.items()):
            response += f"━━ {cat.upper()} ({len(cat_tips)}) ━━\n"
            for tip in cat_tips[:3]:  # Show first 3 per category
                preview = tip['tip_text'][:80] + "..." if len(tip['tip_text']) > 80 else tip['tip_text']
                response += f"  #{tip['id']}: {preview}\n"
            if len(cat_tips) > 3:
                response += f"  ... and {len(cat_tips) - 3} more\n"
            response += "\n"
        
        response += "\n💡 Use: ADMIN VIEW_TIP <id> to see full tip"
        return response
    
    # VIEW SPECIFIC TIP: ADMIN VIEW_TIP <id>
    elif msg_upper.startswith("ADMIN VIEW_TIP"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN VIEW_TIP <tip_id>"
        
        try:
            tip_id = int(parts[2])
            tip = get_tip_by_id(tip_id)
            
            if not tip:
                return f"⚠️ Tip #{tip_id} not found"
            
            status = "✅ Active" if tip['active'] else "❌ Inactive"
            
            return (
                f"📋 *Tip #{tip['id']}*\n\n"
                f"Category: {tip['category']}\n"
                f"Status: {status}\n"
                f"Added: {tip['date_added'][:10]}\n\n"
                f"Text:\n{tip['tip_text']}"
            )
        except ValueError:
            return "⚠️ Invalid tip ID. Must be a number."
    
    # DEACTIVATE TIP: ADMIN REMOVE_TIP <id>
    elif msg_upper.startswith("ADMIN REMOVE_TIP"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN REMOVE_TIP <tip_id>"
        
        try:
            tip_id = int(parts[2])
            success, message = deactivate_tip(tip_id)
            return f"{'✅' if success else '⚠️'} {message}"
        except ValueError:
            return "⚠️ Invalid tip ID. Must be a number."
    
    # ACTIVATE TIP: ADMIN ACTIVATE_TIP <id>
    elif msg_upper.startswith("ADMIN ACTIVATE_TIP"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN ACTIVATE_TIP <tip_id>"
        
        try:
            tip_id = int(parts[2])
            success, message = activate_tip(tip_id)
            return f"{'✅' if success else '⚠️'} {message}"
        except ValueError:
            return "⚠️ Invalid tip ID. Must be a number."
    
    # TIP STATISTICS: ADMIN TIP_STATS [phone_number]
    elif msg_upper.startswith("ADMIN TIP_STATS"):
        parts = msg.split()
        
        if len(parts) > 2:
            # Stats for specific user
            phone_number = parts[2]
            stats = get_user_tip_stats(phone_number)
            
            return (
                f"📊 *Tip Stats for {phone_number}*\n\n"
                f"Total Tips Received: {stats['total_tips_received']}\n"
                f"Last 30 Days: {stats['tips_last_30_days']}\n"
                f"Last Tip Date: {stats['last_tip_date'] or 'Never'}"
            )
        else:
            # Global stats
            stats = get_global_tip_stats()
            
            response = "📊 *Global Tip Statistics*\n\n"
            response += f"Active Tips: {stats['total_active_tips']}\n"
            response += f"Tips Sent Today: {stats['tips_sent_today']}\n"
            response += f"Users Enabled: {stats['users_with_tips_enabled']}\n\n"
            response += "Tips by Category:\n"
            
            for cat, count in stats['tips_by_category'].items():
                response += f"  • {cat}: {count}\n"
            
            return response
    
    # TEST TIP: ADMIN TEST_TIP <phone_number>
    elif msg_upper.startswith("ADMIN TEST_TIP"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN TEST_TIP <phone_number>"
        
        phone_number = parts[2]
        
        try:
            # Get next tip
            tip = get_next_tip_for_user(phone_number)
            
            if not tip:
                return "⚠️ No tips available"
            
            # Send test message
            category_emoji = {
                'motivation': '💪',
                'stress': '🧘',
                'mindfulness': '🧠',
                'sleep': '😴',
                'positivity': '✨',
                'general': '💭'
            }
            
            emoji = category_emoji.get(tip['category'], '💭')
            
            message = (
                f"🧪 *TEST TIP*\n\n"
                f"{emoji} *Mental Wellness Tip:*\n\n"
                f"{tip['tip_text']}\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"Category: {tip['category']}\n"
                f"Tip ID: #{tip['id']}"
            )
            
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=phone_number,
                body=message
            )
            
            return f"✅ Test tip sent to {phone_number}\nCategory: {tip['category']}\nTip ID: #{tip['id']}"
            
        except Exception as e:
            return f"⚠️ Error sending test tip: {str(e)}"
    
    # BROADCAST TIP NOW: ADMIN BROADCAST_TIP
    elif msg_upper.startswith("ADMIN BROADCAST_TIP"):
        try:
            send_daily_mental_health_tips()
            return "✅ Broadcasting tips to all users... Check console for details."
        except Exception as e:
            return f"⚠️ Error broadcasting tips: {str(e)}"
    
     # If no tip command matched
    return "⚠️ Unknown tip command. Type 'ADMIN TIP_HELP' for available commands."


# -------------------------
# ADMIN COMMAND HANDLERS
# -------------------------

def handle_admin_command(sender, incoming_msg):
    """Handle admin commands for user management."""
    
    msg = incoming_msg.upper().strip()

    # ✅ FIX 3: Check tip commands FIRST (more specific)
    if msg.startswith("ADMIN TIP") or msg.startswith("ADMIN ADD_TIP") or \
       msg.startswith("ADMIN LIST_TIPS") or msg.startswith("ADMIN VIEW_TIP") or \
       msg.startswith("ADMIN REMOVE_TIP") or msg.startswith("ADMIN ACTIVATE_TIP") or \
       msg.startswith("ADMIN BROADCAST_TIP") or msg.startswith("ADMIN TEST_TIP"):
        return handle_tip_admin_commands(sender, incoming_msg)
    
    # ADD USER: ADMIN ADD whatsapp:+1234567890 [Name] [Days]
    if msg.startswith("ADMIN ADD"):
        parts = incoming_msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN ADD <phone_number> [name] [expiry_days]\nExample: ADMIN ADD whatsapp:+1234567890 John 30"
        
        phone = parts[2]
        name = parts[3] if len(parts) > 3 else None
        days = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        
        success, message = add_user(phone, name, days)
        return f"{'✅' if success else '⚠️'} {message}"
    
    # REMOVE USER: ADMIN REMOVE whatsapp:+1234567890
    elif msg.startswith("ADMIN REMOVE"):
        parts = incoming_msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN REMOVE <phone_number>"
        
        phone = parts[2]
        success, message = remove_user(phone)
        return f"{'✅' if success else '⚠️'} {message}"
    
    # REACTIVATE USER: ADMIN REACTIVATE whatsapp:+1234567890
    elif msg.startswith("ADMIN REACTIVATE"):
        parts = incoming_msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN REACTIVATE <phone_number>"
        
        phone = parts[2]
        success, message = reactivate_user(phone)
        return f"{'✅' if success else '⚠️'} {message}"
    
    # LIST USERS: ADMIN LIST
    elif msg.startswith("ADMIN LIST"):
        users = list_all_users()
        if not users:
            return "📋 No users in database"
        
        response = "📋 *Authorized Users:*\n\n"
        for user in users[:20]:
            status = "✅" if user['authorized'] else "❌"
            expiry = (
                f" (Expires: {user['expiry_date'].strftime('%Y-%m-%d')})"
                if user.get('expiry_date')
                else ""
            )
            response += f"{status} {user['phone_number']}{expiry}\n"
        
        if len(users) > 20:
            response += f"\n... and {len(users) - 20} more users"
        
        return response
    
    # USER INFO: ADMIN INFO whatsapp:+1234567890
    elif msg.startswith("ADMIN INFO"):
        parts = incoming_msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN INFO <phone_number>"
        
        phone = parts[2]
        user = get_user_info(phone)
        
        if not user:
            return "⚠️ User not found"
        
        status = "Active ✅" if user['authorized'] else "Inactive ❌"
        expiry = user['expiry_date'] if user['expiry_date'] else "No expiry"
        
        return (f"📋 *User Info:*\n"
                f"Phone: {user['phone_number']}\n"
                f"Name: {user['name'] or 'N/A'}\n"
                f"Status: {status}\n"
                f"Added: {user['date_added'][:10]}\n"
                f"Expiry: {expiry}")
    
    # TEST WEEKLY REPORT
    elif msg.startswith("ADMIN TEST_REPORT"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN TEST_REPORT <phone_number>"
        
        phone = parts[2]
        progress = get_weekly_progress(phone)
        
        if not progress:
            return f"📊 No workout data for {phone} in last 7 days"
        
        return (
            f"📊 *Weekly Stats for {phone}*\n\n"
            f"Workouts: {progress['workouts_completed']}\n"
            f"Minutes: {int(progress['total_minutes'])}\n"
            f"Calories: {int(progress['total_calories'])}\n"
            f"Progress: {round(progress['avg_progress'], 1)}%\n"
            f"Goal: {progress['goal']}"
        )
    
    # SEND REPORTS NOW
    elif msg == "ADMIN SEND_REPORTS":
        send_weekly_progress_reports()
        return "✅ Sending weekly reports now... Check console!"

    # SCHEDULER STATUS
    elif msg == "ADMIN SCHEDULER_STATUS":
        if not scheduler or not scheduler.running:
            return "⚠️ Scheduler is NOT running!"
        
        jobs = scheduler.get_jobs()
        response = f"✅ Scheduler is running\n\n"
        response += f"📅 Current Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
        response += f"📅 Current Time (IST): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        response += f"📋 Scheduled Jobs ({len(jobs)}):\n"
        
        for job in jobs:
            next_run = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S UTC') if job.next_run_time else 'Never'
            response += f"\n• {job.name}\n"
            response += f"  Next run: {next_run}\n"
        
        return response
    
    # TEST TIPS NOW
    elif msg == "ADMIN TEST_TIPS_NOW":
        try:
            send_daily_mental_health_tips()
            return "✅ Test tips sent! Check console for details."
        except Exception as e:
            return f"❌ Error: {str(e)}"
    
    # TEST REPORT NOW
    elif msg == "ADMIN TEST_REPORT_NOW":
        try:
            send_weekly_progress_reports()
            return "✅ Test reports sent! Check console for details."
        except Exception as e:
            return f"❌ Error: {str(e)}"
            
    # ADMIN HELP
    elif msg.startswith("ADMIN HELP") or msg == "ADMIN":
        return (
            "🔐 *Admin Commands:*\n\n"
            "📱 USER MANAGEMENT:\n"
            "ADMIN ADD <phone> [name] [days]\n"
            "ADMIN REMOVE <phone>\n"
            "ADMIN REACTIVATE <phone>\n"
            "ADMIN LIST\n"
            "ADMIN INFO <phone>\n\n"
            "💭 MENTAL HEALTH TIPS:\n"
            "ADMIN TIP_HELP\n\n"
            "📊 REPORTS:\n"
            "ADMIN TEST_REPORT <phone>\n"
            "ADMIN SEND_REPORTS\n\n"
            "💪 DAILY WORKOUTS:\n"
            "ADMIN SCHEDULES\n"
            "ADMIN TEST_DAILY <phone>\n"
            "ADMIN RESCHEDULE_ALL\n"
            "ADMIN DISABLE_DAILY <phone>\n\n"
            "⏰ SCHEDULER:\n"
            "ADMIN SCHEDULER_STATUS\n"
            "ADMIN TEST_TIPS_NOW\n"
            "ADMIN TEST_REPORT_NOW\n\n"
            "Example:\n"
            "ADMIN ADD whatsapp:+1234567890 John 30"
        )
        
    # ✅ NEW: LIST WORKOUT SCHEDULES
    elif msg == "ADMIN SCHEDULES":
        from database_pg import get_all_scheduled_users
        
        users = get_all_scheduled_users()
        
        if not users:
            return "📅 No active workout schedules"
        
        response = f"📅 *Active Workout Schedules* ({len(users)}):\n\n"
        
        for user in users[:25]:
            name = user['name'] or "Unknown"
            time = user['preferred_time']
            phone = user['phone_number']
            response += f"• {name}: {time} IST\n  ({phone})\n\n"
        
        if len(users) > 25:
            response += f"... and {len(users) - 25} more"
        
        return response
    
    # ✅ NEW: TEST DAILY WORKOUT
    elif msg.startswith("ADMIN TEST_DAILY"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN TEST_DAILY <phone_number>\n\nExample:\nADMIN TEST_DAILY whatsapp:+1234567890"
        
        phone = parts[2]
        
        try:
            send_daily_workout_plan(phone)
            return f"✅ Test daily workout sent to {phone}\n\nCheck the console for details."
        except Exception as e:
            return f"❌ Error: {str(e)}"
    
    # ✅ NEW: RESCHEDULE ALL WORKOUTS
    elif msg == "ADMIN RESCHEDULE_ALL":
        try:
            reschedule_all_daily_workouts()
            return "✅ Rescheduling all daily workouts...\n\nCheck console for details."
        except Exception as e:
            return f"❌ Error: {str(e)}"
    
    # ✅ NEW: DISABLE USER'S DAILY WORKOUTS
    elif msg.startswith("ADMIN DISABLE_DAILY"):
        parts = msg.split()
        if len(parts) < 3:
            return "⚠️ Usage: ADMIN DISABLE_DAILY <phone_number>"
        
        phone = parts[2]
        
        from database_pg import deactivate_workout_schedule
        success, message = deactivate_workout_schedule(phone)
        
        return f"{'✅' if success else '⚠️'} {message}"

    
    # ✅ FIX 4: Return error if command not recognized
    else:
        return "⚠️ Unknown admin command. Type 'ADMIN HELP' for available commands."

# -------------------------
# Reminder Helper Functions
# -------------------------

# Define IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def parse_reminder_with_llm(user_message, llm):
    """
    Use LLM to intelligently parse reminder requests in ANY format.
    
    Args:
        user_message (str): User's free-form reminder request
        llm: Language model instance (Groq)
        
    Returns:
        tuple: (task, remind_time) or (None, None) if parsing fails
    """
    
    try:
        # Current time in IST for context
        now_ist = datetime.now(IST)
        current_time_str = now_ist.strftime('%Y-%m-%d %H:%M:%S %Z (UTC+5:30)')
        
        # Create extraction prompt
        extraction_prompt = f"""You are a reminder parsing assistant. Extract reminder information from the user's message.

Current time: {current_time_str}

User message: "{user_message}"

Extract and return ONLY valid JSON (no markdown, no explanations):
{{
  "task": "The thing to remind about (clean, natural language)",
  "time_type": "relative" or "absolute",
  "relative_amount": Number or null (for "in X minutes"),
  "relative_unit": "seconds" or "minutes" or "hours" or "days" or null,
  "absolute_hour": Number 0-23 or null (for "at 3:30pm"),
  "absolute_minute": Number 0-59 or null,
  "is_tomorrow": true or false,
  "confidence": 0.0-1.0 (how confident you are in parsing)
}}

Rules:
1. "in 30 minutes" → time_type: "relative", relative_amount: 30, relative_unit: "minutes"
2. "at 3:30pm" → time_type: "absolute", absolute_hour: 15, absolute_minute: 30
3. "at 8am" → time_type: "absolute", absolute_hour: 8, absolute_minute: 0
4. "tomorrow at 9am" → time_type: "absolute", absolute_hour: 9, absolute_minute: 0, is_tomorrow: true
5. "in 2 hours" → time_type: "relative", relative_amount: 2, relative_unit: "hours"

Extract task by removing all time-related words.
confidence should be 1.0 if you're certain, 0.5 if unsure, 0 if can't parse.

Examples:
- "remind me to drink water in 30 minutes" → {{"task": "drink water", "time_type": "relative", "relative_amount": 30, "relative_unit": "minutes", "confidence": 1.0}}
- "set reminder at 4:30pm for workout" → {{"task": "workout", "time_type": "absolute", "absolute_hour": 16, "absolute_minute": 30, "confidence": 1.0}}
- "i want a reminder about calling mom at 6pm" → {{"task": "call mom", "time_type": "absolute", "absolute_hour": 18, "absolute_minute": 0, "confidence": 1.0}}
- "remind me tomorrow at 8am about breakfast" → {{"task": "breakfast", "time_type": "absolute", "absolute_hour": 8, "absolute_minute": 0, "is_tomorrow": true, "confidence": 1.0}}
"""
        
        # Call LLM
        response = llm.invoke([LangChainHumanMessage(content=extraction_prompt)])
        response_text = response.content.strip()
        
        print(f"🤖 LLM Response: {response_text[:200]}...")
        
        # Clean response (remove markdown if present)
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        # Parse JSON
        parsed_data = json.loads(response_text)
        
        # Check confidence
        confidence = parsed_data.get("confidence", 0)
        if confidence < 0.5:
            print(f"⚠️ Low confidence ({confidence}) - rejecting parse")
            return None, None
        
        task = parsed_data.get("task", "").strip()
        if not task or len(task) < 2:
            print(f"⚠️ Invalid task extracted")
            return None, None
        
        # Calculate reminder time
        remind_time = None
        
        if parsed_data.get("time_type") == "relative":
            amount = parsed_data.get("relative_amount")
            unit = parsed_data.get("relative_unit")
            
            if amount and unit:
                if unit == "seconds":
                    remind_time = now_ist + timedelta(seconds=amount)
                elif unit == "minutes":
                    remind_time = now_ist + timedelta(minutes=amount)
                elif unit == "hours":
                    remind_time = now_ist + timedelta(hours=amount)
                elif unit == "days":
                    remind_time = now_ist + timedelta(days=amount)
        
        elif parsed_data.get("time_type") == "absolute":
            hour = parsed_data.get("absolute_hour")
            minute = parsed_data.get("absolute_minute", 0)
            is_tomorrow = parsed_data.get("is_tomorrow", False)
            
            if hour is not None:
                remind_time = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # If in past or tomorrow flag set, move to tomorrow
                if remind_time < now_ist or is_tomorrow:
                    remind_time += timedelta(days=1)
        
        # Validate result
        if not remind_time:
            print(f"⚠️ Could not calculate reminder time")
            return None, None
        
        print(f"✅ LLM Parse Success:")
        print(f"   Task: {task}")
        print(f"   Time: {remind_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"   Confidence: {confidence}")
        
        return task, remind_time
        
    except json.JSONDecodeError as e:
        print(f"❌ JSON Parse Error: {e}")
        print(f"   Response: {response_text}")
        return None, None
    except Exception as e:
        print(f"❌ LLM Parse Error: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def parse_reminder_fallback(message):
    """
    Fallback regex-based parser if LLM fails.
    Handles common formats.
    """
    message = message.lower().strip()
    
    # "in X minutes/hours"
    match = re.search(r"in\s+(\d+)\s*(second|minute|hour|day)s?", message)
    if match:
        amount = int(match.group(1))
        unit = match.group(2) + "s"
        
        task = re.sub(r"in\s+\d+\s*(?:second|minute|hour|day)s?", "", message, flags=re.IGNORECASE)
        task = re.sub(r"^(remind|remind me|set reminder?)\s+(to\s+)?", "", task, flags=re.IGNORECASE)
        task = task.strip() or "Your reminder"
        
        now_ist = datetime.now(IST)
        if "second" in unit:
            remind_time = now_ist + timedelta(seconds=amount)
        elif "minute" in unit:
            remind_time = now_ist + timedelta(minutes=amount)
        elif "hour" in unit:
            remind_time = now_ist + timedelta(hours=amount)
        else:
            remind_time = now_ist + timedelta(days=amount)
        
        print(f"✅ Fallback regex match: {task} in {amount} {unit}")
        return task, remind_time
    
    # "at HH:MM"
    match = re.search(r"at\s+(\d{1,2}):(\d{2})\s*(am|pm)?", message, re.IGNORECASE)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        meridiem = match.group(3).lower() if match.group(3) else None
        
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        
        task = re.sub(r"at\s+\d{1,2}:\d{2}\s*(?:am|pm)?", "", message, flags=re.IGNORECASE)
        task = re.sub(r"^(remind|remind me|set reminder?)\s+(to\s+)?", "", task, flags=re.IGNORECASE)
        task = task.strip() or "Your reminder"
        
        now_ist = datetime.now(IST)
        remind_time = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if remind_time < now_ist:
            remind_time += timedelta(days=1)
        
        print(f"✅ Fallback regex match: {task} at {hour:02d}:{minute:02d}")
        return task, remind_time
    
    print(f"⚠️ Fallback regex: no match")
    return None, None


def parse_reminder_message(message, llm):
    """
    Parse reminder with LLM, fallback to regex if LLM fails.
    
    Args:
        message (str): User's reminder request
        llm: Language model instance
        
    Returns:
        tuple: (task, remind_time) or (None, None)
    """
    
    # Try LLM first
    task, remind_time = parse_reminder_with_llm(message, llm)
    
    if task and remind_time:
        return task, remind_time
    
    # Fallback to regex
    print("⚠️ LLM parsing failed, using fallback regex parser...")
    return parse_reminder_fallback(message)

def schedule_reminder(sender, task, run_time):
    """Schedule a reminder job with IST to UTC conversion."""
    global scheduler
    
    if not scheduler or not scheduler.running:
        print("⚠️ Scheduler not running, cannot schedule reminder")
        return False
    
    try:
        # Convert IST time to UTC for APScheduler
        if run_time.tzinfo is None:
            run_time_utc = run_time.replace(tzinfo=IST).astimezone(timezone.utc)
        else:
            run_time_utc = run_time.astimezone(timezone.utc)
        
        job_id = f"reminder_{sender}_{int(datetime.now().timestamp())}"
        reminder_msg = f"⏰ Reminder: {task}"
        
        scheduler.add_job(
            func=send_message_via_twilio,
            trigger='date',
            run_date=run_time_utc,
            args=[sender, reminder_msg],
            id=job_id,
            name=f'Reminder: {task}',
            replace_existing=True,
            timezone='UTC'
        )
        
        print(f"✅ Reminder scheduled!")
        print(f"   📅 IST: {run_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   🖥️  UTC: {run_time_utc.strftime('%Y-%m-%d %H:%M:%S')}")
        return True
        
    except Exception as e:
        print(f"❌ Error scheduling reminder: {e}")
        import traceback
        traceback.print_exc()
        return False


def clean_response_formatting(text):
    """Remove markdown symbols and clean formatting."""
    
    # Remove bold markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    
    # Remove italic markers
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    
    # Replace markdown bullets with clean dashes
    text = re.sub(r'^[\*•]\s+', '   - ', text, flags=re.MULTILINE)
    
    # Normalize line breaks
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()


def format_bonus_tips(tips):
    """Format bonus tips in clean numbered style."""
    if not tips:
        return ""
    
    section = "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    section += "BONUS TIPS FOR YOU\n\n"
    
    for i, tip in enumerate(tips, 1):
        section += f"Tip {i}: {tip}\n\n"
    
    section += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    
    return section

def smart_chunk(text, max_length=1500):
            if len(text) <= max_length:
                return [text]
            
            chunks = []
            while text:
                if len(text) <= max_length:
                    chunks.append(text)
                    break
                
                # Find the last sentence boundary before max_length
                chunk = text[:max_length]
                
                # Look for sentence endings: . ! ? followed by space or newline
                last_period = max(chunk.rfind('. '), chunk.rfind('.\n'))
                last_exclaim = max(chunk.rfind('! '), chunk.rfind('!\n'))
                last_question = max(chunk.rfind('? '), chunk.rfind('?\n'))
                
                split_pos = max(last_period, last_exclaim, last_question)
                
                # If no sentence boundary found, look for newlines
                if split_pos == -1:
                    split_pos = chunk.rfind('\n')
                
                # If still nothing, split at last space
                if split_pos == -1:
                    split_pos = chunk.rfind(' ')
                
                # If still nothing (no spaces), just split at max_length
                if split_pos == -1:
                    split_pos = max_length - 1
                else:
                    split_pos += 1  # Include the punctuation/newline
                
                chunks.append(text[:split_pos].strip())
                text = text[split_pos:].strip()
            
            return chunks
            
# ========================
# 🆕 GENERAL PLAN FUNCTIONS
# ========================

def generate_general_plan(session):
    """
    Generate a general fitness plan when user skips detailed onboarding.
    Uses available data + sensible defaults.
    
    Args:
        session (dict): User session with basic info
        
    Returns:
        str: Formatted general fitness plan
    """
    
    name = session.get('name', 'there')
    
    # Use available constraints or defaults
    duration = session.get('workout_duration') or "30 minutes"
    location = session.get('workout_location') or "Home"
    goal = session.get('fitness_goal') or "overall fitness"
    diet_pref = session.get('diet_preference') or "mixed"
    
    # Parse duration for calculation
    try:
        if isinstance(duration, str):
            duration_mins = int(''.join(filter(str.isdigit, duration.split('-')[0]))) or 30
        else:
            duration_mins = 30
    except:
        duration_mins = 30
    
    # Build plan based on goal
    goal_lower = str(goal).lower()
    
    if "muscle" in goal_lower or "bulk" in goal_lower or "build" in goal_lower:
        plan = generate_muscle_building_plan(name, duration_mins, location, diet_pref)
    elif "weight" in goal_lower or "fat" in goal_lower or "lose" in goal_lower:
        plan = generate_weight_loss_plan(name, duration_mins, location, diet_pref)
    elif "endurance" in goal_lower or "cardio" in goal_lower or "stamina" in goal_lower:
        plan = generate_cardio_plan(name, duration_mins, location, diet_pref)
    else:
        plan = generate_balanced_plan(name, duration_mins, location, diet_pref)
    
    return plan


def generate_muscle_building_plan(name, duration, location, diet_pref):
    """Generate muscle building plan."""
    
    if location.lower() == "gym":
        exercises = [
            "Barbell Bench Press: 4 sets x 6-8 reps",
            "Barbell Squats: 4 sets x 6-8 reps",
            "Barbell Rows: 4 sets x 6-8 reps",
            "Overhead Press: 3 sets x 8-10 reps",
            "Deadlifts: 3 sets x 5-6 reps"
        ]
    else:  # Home
        exercises = [
            "Push-ups: 3 sets x 12-15 reps",
            "Squats: 3 sets x 15-20 reps",
            "Dumbbell Rows: 3 sets x 12-15 reps",
            "Pike Push-ups: 3 sets x 10-12 reps",
            "Lunges: 2 sets x 12 reps per leg"
        ]
    
    plan = (
        f"💪 MUSCLE BUILDING PLAN FOR {name.upper()}\n\n"
        f"Duration: {duration} minutes\n"
        f"Location: {location}\n\n"
        
        f"WARM-UP (5 minutes)\n\n"
        f"   - Light cardio (jumping jacks, jogging in place): 2 minutes\n"
        f"   - Arm circles and shoulder rolls: 1 minute\n"
        f"   - Bodyweight squats: 1 minute\n"
        f"   - Wrist and ankle rotations: 1 minute\n\n"
        
        f"MAIN WORKOUT ({duration - 10} minutes)\n\n"
    )
    
    for i, exercise in enumerate(exercises, 1):
        plan += f"   - {exercise}\n"
    
    plan += (
        f"\n\nCOOL DOWN (5 minutes)\n\n"
        f"   - Static stretching (hamstrings, quads, chest, shoulders)\n"
        f"   - Deep breathing: 1 minute\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"NUTRITION PLAN\n\n"
        f"Daily Targets:\n"
        f"   - Protein: 160-180g (2g per kg bodyweight)\n"
        f"   - Calories: 2500-2800 kcal\n"
        f"   - Carbs: 300-350g\n"
        f"   - Fats: 70-80g\n"
        f"   - Water: 3-4 liters\n\n"
        
        f"DIET PLAN\n\n"
        f"Breakfast:\n"
        f"   - 3-4 eggs + 2 toast + banana + milk\n\n"
        
        f"Mid-Morning Snack:\n"
        f"   - Protein shake (whey + milk) with oats\n\n"
        
        f"Lunch:\n"
        f"   - 200g chicken/fish + 1 cup rice + vegetables\n\n"
        
        f"Pre-Workout (30 mins before):\n"
        f"   - 1 banana + 1 tbsp peanut butter\n\n"
        
        f"Post-Workout (within 30 mins):\n"
        f"   - Protein shake + dextrose/banana\n\n"
        
        f"Dinner:\n"
        f"   - 200g chicken/paneer + 1 cup rice + salad\n\n"
        
        f"Bedtime Snack:\n"
        f"   - Greek yogurt or casein shake\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"RECOVERY & TIPS\n\n"
        f"   - Sleep: 7-8 hours daily\n"
        f"   - Rest days: 1-2 per week\n"
        f"   - Progressive overload: Increase weight by 2-5% each week\n"
        f"   - Track your workouts in a notebook\n"
        f"   - Consistency is key! 💪\n"
    )
    
    return plan


def generate_weight_loss_plan(name, duration, location, diet_pref):
    """Generate weight loss plan."""
    
    if location.lower() == "gym":
        exercises = [
            "Treadmill running: 10 minutes (intervals)",
            "Rowing machine: 8 minutes",
            "Burpees: 3 sets x 10 reps",
            "Jump rope: 3 sets x 1 minute",
            "Mountain climbers: 3 sets x 15 reps",
            "Cycling: 5 minutes (moderate pace)"
        ]
    else:  # Home
        exercises = [
            "Jumping jacks: 2 minutes",
            "Burpees: 3 sets x 8-10 reps",
            "High knees: 2 minutes",
            "Mountain climbers: 3 sets x 15 reps",
            "Jump squats: 3 sets x 12 reps",
            "Plank: 3 sets x 30-45 seconds"
        ]
    
    plan = (
        f"🔥 WEIGHT LOSS PLAN FOR {name.upper()}\n\n"
        f"Duration: {duration} minutes\n"
        f"Location: {location}\n\n"
        
        f"WARM-UP (3-5 minutes)\n\n"
        f"   - Light jogging in place: 2 minutes\n"
        f"   - Arm circles and leg swings: 2 minutes\n\n"
        
        f"MAIN CARDIO WORKOUT ({duration - 10} minutes)\n\n"
    )
    
    for i, exercise in enumerate(exercises, 1):
        plan += f"   - {exercise}\n"
    
    plan += (
        f"\n\nCOOL DOWN (5 minutes)\n\n"
        f"   - Walking at slow pace\n"
        f"   - Stretching (all major muscle groups)\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"NUTRITION PLAN\n\n"
        f"Daily Targets:\n"
        f"   - Calories: 1800-2000 kcal (500 kcal deficit)\n"
        f"   - Protein: 120-140g (maintains muscle)\n"
        f"   - Carbs: 150-180g (complex carbs)\n"
        f"   - Fats: 50-60g (healthy fats)\n"
        f"   - Water: 3-4 liters\n\n"
        
        f"DIET PLAN\n\n"
        f"Breakfast:\n"
        f"   - Oatmeal (50g) + berries + milk\n\n"
        
        f"Mid-Morning Snack:\n"
        f"   - Apple + almond butter (1 tbsp)\n\n"
        
        f"Lunch:\n"
        f"   - 150g chicken/fish + 1/2 cup brown rice + vegetables\n\n"
        
        f"Pre-Workout Snack:\n"
        f"   - Banana or energy bar\n\n"
        
        f"Post-Workout:\n"
        f"   - Protein shake with water\n\n"
        
        f"Dinner:\n"
        f"   - 150g lean protein + sweet potato + salad\n\n"
        
        f"Evening Snack:\n"
        f"   - Green tea or light milk (if hungry)\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"WEIGHT LOSS TIPS\n\n"
        f"   - Create calorie deficit: 500 cal/day = 0.5kg/week loss\n"
        f"   - Drink water before meals\n"
        f"   - Sleep 7-8 hours (poor sleep increases hunger)\n"
        f"   - Avoid sugary drinks and processed foods\n"
        f"   - Do cardio 4-5 times per week\n"
        f"   - Track your progress weekly\n"
    )
    
    return plan


def generate_cardio_plan(name, duration, location, diet_pref):
    """Generate cardio/endurance plan."""
    
    if location.lower() == "gym":
        exercises = [
            "Treadmill running: 15 minutes (steady pace)",
            "Rowing machine: 8 minutes",
            "Cycling: 7 minutes",
            "Jump rope: 2 sets x 1.5 minutes",
            "Elliptical: 5 minutes (cooldown)"
        ]
    else:  # Home
        exercises = [
            "Jogging in place: 5 minutes",
            "Jumping jacks: 2 minutes",
            "High knees running: 3 minutes",
            "Rope skipping: 2 sets x 1.5 minutes",
            "Running stairs: 3 sets x 30 seconds",
            "Cool-down walk: 5 minutes"
        ]
    
    plan = (
        f"❤️ CARDIO & ENDURANCE PLAN FOR {name.upper()}\n\n"
        f"Duration: {duration} minutes\n"
        f"Location: {location}\n\n"
        
        f"WARM-UP (5 minutes)\n\n"
        f"   - Light jogging: 2 minutes\n"
        f"   - Dynamic stretches: 3 minutes\n\n"
        
        f"MAIN CARDIO WORKOUT ({duration - 10} minutes)\n\n"
    )
    
    for i, exercise in enumerate(exercises, 1):
        plan += f"   - {exercise}\n"
    
    plan += (
        f"\n\nCOOL DOWN (5 minutes)\n\n"
        f"   - Slow walking: 2 minutes\n"
        f"   - Static stretching: 3 minutes\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"NUTRITION PLAN\n\n"
        f"Daily Targets:\n"
        f"   - Calories: 2200-2500 kcal (endurance friendly)\n"
        f"   - Protein: 100-120g\n"
        f"   - Carbs: 300-350g (fuel for endurance)\n"
        f"   - Fats: 60-70g\n"
        f"   - Water: 3-4 liters minimum\n\n"
        
        f"DIET PLAN\n\n"
        f"Breakfast:\n"
        f"   - Whole wheat toast + eggs + orange juice\n\n"
        
        f"Mid-Morning Snack:\n"
        f"   - Granola bar + banana\n\n"
        
        f"Lunch:\n"
        f"   - 150g chicken + 1 cup rice + vegetables\n\n"
        
        f"Pre-Workout (1-2 hours before):\n"
        f"   - Banana + honey or energy drink\n\n"
        
        f"Post-Workout:\n"
        f"   - Chocolate milk (carbs + protein recovery)\n\n"
        
        f"Dinner:\n"
        f"   - 150g fish/lean meat + pasta + salad\n\n"
        
        f"Bedtime:\n"
        f"   - Light snack (banana or yogurt)\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"ENDURANCE BUILDING TIPS\n\n"
        f"   - Run 3-4 times per week\n"
        f"   - Increase distance/time by 10% weekly\n"
        f"   - Do 1 long slow run per week\n"
        f"   - Cross-train (cycling, swimming) 1-2x/week\n"
        f"   - Hydrate constantly during long runs\n"
        f"   - Recovery is important: 7-8 hours sleep\n"
    )
    
    return plan


def generate_balanced_plan(name, duration, location, diet_pref):
    """Generate balanced overall fitness plan."""
    
    if location.lower() == "gym":
        exercises = [
            "Warm-up cardio: 5 minutes",
            "Bench press: 3 sets x 10 reps",
            "Squats: 3 sets x 12 reps",
            "Rows: 3 sets x 10 reps",
            "Leg press: 2 sets x 12 reps",
            "Core work (planks): 3 sets x 30 seconds",
            "Cardio cooldown: 5 minutes"
        ]
    else:  # Home
        exercises = [
            "Jumping jacks: 2 minutes (warm-up)",
            "Push-ups: 3 sets x 12-15 reps",
            "Squats: 3 sets x 15-20 reps",
            "Dumbbell rows: 3 sets x 12 reps",
            "Lunges: 2 sets x 10 per leg",
            "Plank: 3 sets x 30-45 seconds",
            "Light cardio (jogging): 3 minutes"
        ]
    
    plan = (
        f"⚡ BALANCED FITNESS PLAN FOR {name.upper()}\n\n"
        f"Duration: {duration} minutes\n"
        f"Location: {location}\n\n"
        
        f"WARM-UP (5 minutes)\n\n"
        f"   - Light cardio: 2 minutes\n"
        f"   - Dynamic stretches: 3 minutes\n\n"
        
        f"MAIN WORKOUT ({duration - 10} minutes)\n\n"
    )
    
    for i, exercise in enumerate(exercises, 1):
        plan += f"   - {exercise}\n"
    
    plan += (
        f"\n\nCOOL DOWN (5 minutes)\n\n"
        f"   - Walking: 2 minutes\n"
        f"   - Static stretching: 3 minutes\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"NUTRITION PLAN\n\n"
        f"Daily Targets:\n"
        f"   - Calories: 2000-2300 kcal (maintenance)\n"
        f"   - Protein: 120-150g\n"
        f"   - Carbs: 200-250g\n"
        f"   - Fats: 60-70g\n"
        f"   - Water: 3 liters\n\n"
        
        f"DIET PLAN\n\n"
        f"Breakfast:\n"
        f"   - Oatmeal + eggs + whole wheat toast\n\n"
        
        f"Mid-Morning Snack:\n"
        f"   - Apple + almonds (10-12)\n\n"
        
        f"Lunch:\n"
        f"   - 150g chicken/paneer + 1 cup rice + vegetables\n\n"
        
        f"Pre-Workout:\n"
        f"   - Banana or granola bar\n\n"
        
        f"Post-Workout:\n"
        f"   - Protein shake or yogurt\n\n"
        
        f"Dinner:\n"
        f"   - 150g fish/meat + sweet potato + salad\n\n"
        
        f"Evening:\n"
        f"   - Green tea or light milk\n\n"
        
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"BALANCED FITNESS TIPS\n\n"
        f"   - Exercise 4-5 days per week\n"
        f"   - Mix strength training (3x) with cardio (2x)\n"
        f"   - Rest 1-2 days per week\n"
        f"   - Sleep 7-8 hours daily\n"
        f"   - Eat whole foods and avoid ultra-processed items\n"
        f"   - Stay hydrated throughout the day\n"
        f"   - Progress gradually and track results\n"
    )
    
    return plan


# ========================
# INTEGRATION FUNCTION
# ========================

def handle_skip_and_generate_plan(sender, session):
    """
    When user skips all remaining inputs, generate general plan and send.
    Call this when user finishes basic info but skips everything else.
    
    Args:
        sender (str): User's phone number
        session (dict): User's session dictionary
    """
    
    # Mark profile as minimally completed
    session["onboarding_step"] = "done"
    session["profile_completed"] = True
    session["profile_confirmed"] = True
    
    # Generate general plan
    plan = generate_general_plan(session)
    
    # Add message to history
    session["messages"].append(HumanMessage(
        content=f"Create my first personalized fitness plan based on my profile."
    ))
    
    # Send plan
    try:
        # Split into chunks if needed
        chunks = smart_chunk(plan, 1500)
        
        for idx, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                body = f"(Part {idx}/{len(chunks)})\n\n{chunk}"
            else:
                body = chunk
            
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=sender,
                body=body
            )
            
            print(f"✅ General plan part {idx}/{len(chunks)} sent")
        
        # Send follow-up message
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=sender,
            body=(
                "This is a general plan based on your goal.\n\n"
                "Feel free to ask me to:\n"
                "   • Modify any exercises\n"
                "   • Change meal suggestions\n"
                "   • Adjust workout duration\n"
                "   • Add any restrictions\n\n"
                "Just tell me what you'd like to change! 💪"
            )
        )
        
    except Exception as e:
        print(f"❌ Error sending general plan: {e}")
        try:
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=sender,
                body="⚠️ Error generating plan. Please try again."
            )
        except:
            pass

# -------------------------
# Helper: Check if message is fitness-related
# -------------------------
def is_fitness_related(message):
    """Check if message is fitness-related with broader keyword matching."""
    message_lower = message.lower()
    
    # Expanded fitness keywords
    fitness_keywords = [
        "workout", "diet", "gym", "exercise", "yoga", "health", "fitness",
        "calories", "nutrition", "training", "protein", "cardio", "strength",
        "weight", "muscle", "fat", "run", "walk", "jog", "swim", "cycle",
        "stretch", "warm", "cool", "rest", "recovery", "sleep", "meal",
        "food", "eat", "drink", "water", "supplement", "vitamin", "carb",
        "plan", "routine", "schedule", "goal", "body", "abs", "leg", "arm",
        "chest", "back", "shoulder", "core", "squat", "push", "pull", "lift",
        "rep", "set", "intensity", "duration", "time", "minute", "hour",
        "injury", "pain", "sore", "tired", "energy", "motivation", "progress"
    ]
    
    # Question words - allow fitness-related questions
    question_words = ["what", "how", "why", "when", "where", "can", "should", 
                      "could", "would", "is", "are", "do", "does", "tell", "show"]
    
    # Check if it's a fitness keyword OR a question (likely fitness-related in context)
    has_fitness_keyword = any(word in message_lower for word in fitness_keywords)
    is_question = any(message_lower.startswith(word) or f" {word} " in message_lower 
                     for word in question_words)
    
    # Also allow short messages (likely follow-ups) after onboarding is done
    is_short_followup = len(message.split()) <= 5
    
    return has_fitness_keyword or (is_question and is_short_followup)

# -------------------------
# Background reply processor (UPDATED)
# -------------------------
def process_and_reply(sender, is_initial_plan=False, incoming_msg=""):
    try:
        session = user_sessions[sender]

        # Prepare system + context
        system_context = SystemMessage(
            content=(
                f"User's details:\n"
                f"- Name: {session['name']}\n"
                f"- Age: {session['age']}\n"
                f"- Gender: {session['gender']}\n"
                f"- Weight: {session['weight']}\n"
                f"- Height: {session['height']}\n"
                f"- Goal: {session['fitness_goal']}\n\n"
                
                f"🚨 STRICT CONSTRAINTS - DO NOT VIOLATE:\n"
                f"- Medical: {session.get('medical_conditions', 'None')} "
                f"{'(adjust recommendations accordingly)' if session.get('medical_conditions') != 'None' else ''}\n"
                f"- Injuries: {session.get('injuries', 'None')} "
                f"{'(provide modified exercises, avoid aggravating movements)' if session.get('injuries') != 'None' else ''}\n"
                f"- Allergies: {session.get('allergies', 'None')} "
                f"{'(NEVER include these foods in diet plan)' if session.get('allergies') != 'None' else ''}\n"
                f"- Workout Duration: {session.get('workout_duration', 'Not specified')} "
                f"(NEVER exceed this time)\n"
                f"- Location: {session.get('workout_location', 'Not specified')} "
                f"(Home = bodyweight/resistance bands/dumbbells only, NO barbells/machines)\n"
                f"- Exercises to Avoid: {session.get('exercises_to_avoid', 'None')} "
                f"(NEVER include these or similar movements)\n"
                f"- Diet Preference: {session.get('diet_preference', 'Not specified')}\n"
                f"- Activity Level: {session.get('activity_level', 'Not specified')}\n"
                f"- Stress Level: {session.get('stress_level', 'Not specified')}\n\n"
                f"Request type: {'INITIAL PLAN - Provide a complete plan for TODAY' if is_initial_plan else 'FOLLOW-UP QUESTION - Answer conversationally'}.\n\n"
                "Instructions:\n"
                + ("- Create a complete workout, nutrition, and diet plan for TODAY\n"
                   "- Clearly label it as 'Today's Workout Plan' at the top\n"
                   "- Include estimated total workout time in minutes\n"
                   "- Nutrition Plan should have macro targets (protein, calories, carbs, fats, water)\n"
                   "- Diet Plan should have actual meal suggestions (breakfast, lunch, dinner, snacks)\n"
                   "- Adjust based on user's time restrictions and injuries" if is_initial_plan else 
                   "- Answer the user's question directly and conversationally\n"
                   "- Reference their goals and restrictions when relevant\n"
                   "- Keep response concise and helpful")
            )
        )

        state = {"messages": [fitness_system_prompt, system_context] + session["messages"]}
        result = graph.invoke(state)
        response_text = result["messages"][-1].content.strip()

        # === ADD PERSONALIZED BONUS TIPS (Only for initial/today's plan) ===
        # Clean the response formatting
        response_text = clean_response_formatting(response_text)
        
        # Add personalized bonus tips (only for initial/today's plan)
        msg_lower = incoming_msg.lower() if incoming_msg else ""
        if is_initial_plan or "today" in msg_lower or "plan" in msg_lower or "workout" in msg_lower:
            bonus_tips = get_personalized_bonus_tips(session)
            if bonus_tips:
                response_text += format_bonus_tips(bonus_tips)

        # Only add reminder prompt and schedule motivational message for initial plans
        if is_initial_plan:
            # Extract Estimated Workout Time
            match_time = re.search(r"Estimated Time:\s*~?(\d+)\s*minutes?", response_text, re.IGNORECASE)
            workout_minutes = int(match_time.group(1)) if match_time else None
            calories_burned = None
            progress_percent = None

            # Estimate Calories Burned & Progress
            if workout_minutes and session.get("weight") and session.get("fitness_goal"):
                try:
                    weight = float(re.findall(r"\d+", str(session["weight"]))[0])
                    goal = str(session["fitness_goal"]).lower()

                    if "muscle" in goal:
                        MET = 8
                    elif "weight" in goal or "fat" in goal:
                        MET = 6
                    elif "cardio" in goal:
                        MET = 7
                    else:
                        MET = 5

                    calories_burned = int(workout_minutes * MET * 3.5 * weight / 200)
                    progress_percent = min(round(workout_minutes / 10, 1), 100)

                    # 🆕 Save workout to database
                    log_workout_completion(
                        sender, 
                        workout_minutes, 
                        calories_burned, 
                        progress_percent, 
                        session["fitness_goal"]
                    )

                    # Update streak tracking
                    current_streak, is_new_record, broke_streak = update_workout_streak(sender)
                    
                    # Store in session for motivational message
                    session['latest_streak'] = {
                        'current': current_streak,
                        'is_record': is_new_record,
                        'broke': broke_streak
                    }

                except Exception as e:
                    print("Calorie calculation error:", e)

            # Add Reminder Help Prompt
            if "would you like to set any reminders" not in response_text.lower():
                response_text += (
                    "\n\nWould you like to set any reminders for your workouts or meals?\n\n"
                    "You can say:\n"
                    "   • Remind me to drink water in 30 minutes\n"
                    "   • Set a reminder at 4:30pm for workout"
                )

            # Schedule Motivational Message After Workout
            if workout_minutes:
                schedule_motivational_message(
                    sender, 
                    session, 
                    workout_minutes, 
                    calories_burned, 
                    progress_percent
                )

        # Send Main LLM Response
        # Smart chunking: split at sentence boundaries, not mid-sentence
        

        chunks = smart_chunk(response_text, 1500)
        session["messages"].append(result["messages"][-1])

        total_parts = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            if total_parts > 1:
                body = f"(Part {idx}/{total_parts})\n\n{chunk}"
            else:
                body = chunk

            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=sender,
                body=body
            )
            print(f"DEBUG reply part {idx}/{total_parts}: {len(chunk)} chars")

    except Exception as e:
        print("Error in process_and_reply:", e)
        # Send error message to user
        try:
            client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=sender,
                body="⚠️ Sorry, I encountered an error. Please try asking your question again."
            )
        except:
            pass

"""
🆕 ENHANCED EXTRACTION FUNCTIONS
Add these to your app.py file to handle new onboarding steps
These use LLM to extract structured data from free-form text
"""

import json
from langchain_core.messages import HumanMessage as LangChainHumanMessage


# =====================
# 🆕 ENHANCED EXTRACTION PROMPTS
# =====================

health_extraction_prompt = """You are a health data extraction assistant. Extract health information from free-form text.
Return ONLY valid JSON, no extra text.

Extract these fields if mentioned:
- medical_conditions: List of medical conditions (PCOD/PCOS, diabetes, thyroid, hypertension, asthma, heart issues, etc.) or "None"
- injuries: Physical injuries or limitations (knee pain, back pain, flat foot, shoulder issues, etc.) or "None"  
- allergies: Food allergies (dairy, nuts, gluten, soy, eggs, shellfish, etc.) or "None"

Be flexible with variations:
- "I have PCOD" → medical_conditions: "PCOD"
- "bad knees" → injuries: "Knee pain"
- "no allergies" → allergies: "None"
- "diabetic" → medical_conditions: "Diabetes"

Return format:
{
  "medical_conditions": "value or None",
  "injuries": "value or None",
  "allergies": "value or None"
}
"""

lifestyle_extraction_prompt = """You are a lifestyle data extraction assistant. Extract lifestyle information from free-form text.
Return ONLY valid JSON, no extra text.

Extract these fields if mentioned:
- diet_preference: Type of diet (Vegetarian/Non-vegetarian/Vegan/Pescatarian/Eggetarian)
- activity_level: Daily activity outside workout (Sedentary/Lightly Active/Moderately Active/Very Active)
- stress_level: Current stress level (Low/Moderate/High or 1-10 scale)

Be flexible with variations:
- "I'm veg" → diet_preference: "Vegetarian"
- "mostly sitting at desk" → activity_level: "Sedentary"
- "very stressed" → stress_level: "High"
- "I walk a lot" → activity_level: "Moderately Active"

Return format:
{
  "diet_preference": "value or null",
  "activity_level": "value or null",
  "stress_level": "value or null"
}
"""

workout_prefs_extraction_prompt = """You are a workout preferences extraction assistant. Extract workout information from free-form text.
Return ONLY valid JSON, no extra text.

Extract these fields if mentioned:
- workout_duration: Time available (15-20 minutes/30-45 minutes/45-60 minutes/60+ minutes/Varies)
- workout_location: Where they workout (Home/Gym/Outdoor/Mixed)
- workout_time: Preferred time - PRESERVE SPECIFIC TIMES (Early Morning/Morning/Afternoon/Evening/Night/Flexible OR specific time like "8am", "6:30pm")
- exercises_to_avoid: Exercises they dislike (burpees, running, planks, jumping, squats, etc.) or "None"

CRITICAL RULES FOR workout_time:
- If user provides a SPECIFIC time (like "8am", "6pm", "6:30pm"), return that EXACT time
- If user says "morning 8am" or "evening 6pm", return the SPECIFIC time ("8am", "6pm") NOT the general word
- ONLY return general times (Morning/Evening/etc) if NO specific time is mentioned
- "flexible", "anytime", "whenever" → return "Flexible"

Be flexible with variations:
- "30 mins" → workout_duration: "30-45 minutes"
- "at home" → workout_location: "Home"
- "evening 6pm" → workout_time: "6pm" ✅ (NOT "Evening")
- "morning 8am" → workout_time: "8am" ✅ (NOT "Morning")
- "6:30 in the evening" → workout_time: "6:30pm" ✅
- "evening" → workout_time: "Evening" (only if no specific time)
- "no preferred time" → workout_time: "Flexible"
- "anytime" → workout_time: "Flexible"
- "flexible" → workout_time: "Flexible"
- "whenever" → workout_time: "Flexible"
- "hate burpees and running" → exercises_to_avoid: "Burpees, Running"
- "no restrictions" → exercises_to_avoid: "None"

EXAMPLES:
- "30 minutes, home, morning 8am" → {"workout_time": "8am"} ✅
- "45 mins, gym, evening 6:30pm" → {"workout_time": "6:30pm"} ✅
- "1 hour, home, morning" → {"workout_time": "Morning"} (no specific time given)
- "30 min, gym, anytime" → {"workout_time": "Flexible"}

Return format:
{
  "workout_duration": "value or null",
  "workout_location": "value or null",
  "workout_time": "value or Flexible",
  "exercises_to_avoid": "value or null"
}
"""


# =====================
# 🆕 ENHANCED EXTRACTION FUNCTIONS
# =====================

def extract_health_info(user_message, llm):
    """
    Extract health information (medical conditions, injuries, allergies).
    
    Args:
        user_message: User's input text
        llm: Language model instance
        
    Returns:
        dict: Extracted health data
    """
    try:
        extraction_prompt = f"""Extract health information from this message:
"{user_message}"

Return ONLY valid JSON with no additional text or markdown."""
        
        messages = [
            LangChainHumanMessage(content=health_extraction_prompt),
            LangChainHumanMessage(content=extraction_prompt)
        ]
        
        response = llm.invoke(messages)
        response_text = response.content.strip()
        
        # Clean response
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        extracted_data = json.loads(response_text)
        
        # Ensure all keys exist
        default_data = {
            "medical_conditions": None,
            "injuries": None,
            "allergies": None
        }
        
        result = {**default_data, **extracted_data}
        
        print(f"✅ Extracted health data: {result}")
        return result
        
    except Exception as e:
        print(f"❌ Health extraction error: {e}")
        return {
            "medical_conditions": None,
            "injuries": None,
            "allergies": None
        }


def extract_lifestyle_info(user_message, llm):
    """
    Extract lifestyle information (diet, activity level, stress).
    
    Args:
        user_message: User's input text
        llm: Language model instance
        
    Returns:
        dict: Extracted lifestyle data
    """
    try:
        extraction_prompt = f"""Extract lifestyle information from this message:
"{user_message}"

Return ONLY valid JSON with no additional text or markdown."""
        
        messages = [
            LangChainHumanMessage(content=lifestyle_extraction_prompt),
            LangChainHumanMessage(content=extraction_prompt)
        ]
        
        response = llm.invoke(messages)
        response_text = response.content.strip()
        
        # Clean response
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        extracted_data = json.loads(response_text)
        
        # Ensure all keys exist
        default_data = {
            "diet_preference": None,
            "activity_level": None,
            "stress_level": None
        }
        
        result = {**default_data, **extracted_data}
        
        print(f"✅ Extracted lifestyle data: {result}")
        return result
        
    except Exception as e:
        print(f"❌ Lifestyle extraction error: {e}")
        return {
            "diet_preference": None,
            "activity_level": None,
            "stress_level": None
        }


def extract_workout_prefs(user_message, llm):
    """
    Extract workout preferences (duration, location, time, exercises to avoid).
    
    Args:
        user_message: User's input text
        llm: Language model instance
        
    Returns:
        dict: Extracted workout preferences
    """
    try:
        extraction_prompt = f"""Extract workout preferences from this message:
"{user_message}"

Return ONLY valid JSON with no additional text or markdown."""
        
        messages = [
            LangChainHumanMessage(content=workout_prefs_extraction_prompt),
            LangChainHumanMessage(content=extraction_prompt)
        ]
        
        response = llm.invoke(messages)
        response_text = response.content.strip()
        
        # Clean response
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        extracted_data = json.loads(response_text)
        
        # Ensure all keys exist
        default_data = {
            "workout_duration": None,
            "workout_location": None,
            "workout_time": None,
            "exercises_to_avoid": None
        }
        
        result = {**default_data, **extracted_data}
        
        print(f"✅ Extracted workout prefs: {result}")
        return result
        
    except Exception as e:
        print(f"❌ Workout prefs extraction error: {e}")
        return {
            "workout_duration": None,
            "workout_location": None,
            "workout_time": None,
            "exercises_to_avoid": None
        }


# =====================
# 🆕 VALIDATION HELPERS
# =====================

def validate_required_fields(extracted_data, step):
    """
    Check if all required fields for a step are present.
    
    Args:
        extracted_data: Extracted data dict
        step: Onboarding step name
        
    Returns:
        tuple: (is_valid, missing_fields)
    """
    required_fields = {
        "basic": ["name", "age", "gender"],
        "personalize": ["weight", "height", "fitness_goal"],
        "health": [],  # All optional
        "lifestyle": ["diet_preference", "activity_level", "stress_level"],
        "workout_prefs": ["workout_duration", "workout_location", "workout_time"]
    }
    
    required = required_fields.get(step, [])
    missing = [field for field in required if not extracted_data.get(field)]
    
    return (len(missing) == 0, missing)


def get_missing_fields_message(missing_fields, step):
    """
    Generate user-friendly message for missing fields.
    
    Args:
        missing_fields: List of missing field names
        step: Onboarding step name
        
    Returns:
        str: User-friendly error message
    """
    if not missing_fields:
        return None
    
    field_prompts = {
        "basic": "Please provide: Name, Age, Gender\n\nExample: John, 25, Male",
        "personalize": "Please provide: Weight, Height, Fitness Goal\n\nExample: 75kg, 5'10\", Build Muscle",
        "lifestyle": "Please provide: Diet preference, Activity level, Stress level\n\nExample: Vegetarian, Mostly sitting, Medium stress",
        "workout_prefs": "Please provide: Workout duration, Location, Preferred time\n\nExample: 30 minutes, Home, Evening 6pm"
    }
    
    message = f"⚠️ I couldn't extract: {', '.join(missing_fields)}\n\n"
    message += field_prompts.get(step, "Please provide the missing information.")
    
    return message
    
@app.route("/scheduler-status")
def scheduler_status():
    """Check scheduler status - for debugging."""
    global scheduler
    
    # Handle case where scheduler might not be initialized
    if scheduler is None:
        return jsonify({
            "status": "not_initialized",
            "running": False,
            "error": "Scheduler was never initialized",
            "jobs": []
        }), 500
    
    try:
        jobs = []
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None
            })
        
        return jsonify({
            "status": "running" if scheduler.running else "stopped",
            "running": scheduler.running,
            "current_time_utc": datetime.utcnow().isoformat(),
            "jobs_count": len(jobs),
            "jobs": jobs
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "running": False,
            "error": str(e),
            "jobs": []
        }), 500
    
# -------------------------
# Webhook for WhatsApp
# -------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from database_pg import calculate_bmi, get_bmi_category
    
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From")
    print(f"📩 Incoming from {sender}: {incoming_msg}")

    # =============================
    # 🔐 AUTHENTICATION CHECK
    # =============================
    
    # ✅ FIX 1: Check admin commands FIRST, before authorization check
    if incoming_msg.upper().startswith("ADMIN"):
        # Check if user is admin
        if not is_admin(sender):
            resp = MessagingResponse()
            resp.message(
                "⛔ *Access Denied*\n\n"
                "You don't have admin privileges.\n\n"
                f"Contact: {ADMIN_CONTACT}"
            )
            log_auth_attempt(sender, "unauthorized_admin_attempt", success=False)
            return str(resp)
        
        # User is admin, process admin command
        admin_response = handle_admin_command(sender, incoming_msg)
        if admin_response:
            resp = MessagingResponse()
            resp.message(admin_response)
            log_auth_attempt(sender, "admin_command", success=True)
            return str(resp)
    
    # Now check regular user authorization (non-admin commands)
    if not is_user_authorized(sender):
        log_auth_attempt(sender, "unauthorized_access", success=False)
        resp = MessagingResponse()
        resp.message(
            f"⛔ *Access Denied*\n\n"
            f"Your number is not authorized to use NexiFit.\n\n"
            f"Please contact the admin to get access:\n"
            f"📧 {ADMIN_CONTACT}"
        )
        print(f"❌ Unauthorized access attempt: {sender}")
        return str(resp)
    
    # Log successful authentication
    log_auth_attempt(sender, "authorized_access", success=True)
    
    # =============================
    # ONBOARDING & CONVERSATION
    # =============================

    if sender not in user_sessions:
        # Check if user has completed profile in database
        profile = get_user_profile(sender)
        
        if profile and profile.get("profile_completed"):
            # ✅ User has completed profile - restore full session from DB
            user_sessions[sender] = {
                "messages": [],
                "onboarding_step": "done",
                "profile_completed": True,
                "profile_confirmed": True,
                "name": profile.get('name'),
                "age": profile.get('age'),
                "gender": profile.get('gender'),
                "weight": profile.get('weight'),
                "height": profile.get('height'),
                "fitness_goal": profile.get('fitness_goal'),
                "medical_conditions": profile.get('medical_conditions'),
                "injuries": profile.get('injuries'),
                "allergies": profile.get('allergies'),
                "diet_preference": profile.get('diet_preference'),
                "activity_level": profile.get('activity_level'),
                "stress_level": profile.get('stress_level'),
                "workout_duration": profile.get('workout_duration'),
                "workout_location": profile.get('workout_location'),
                "workout_time": profile.get('workout_time'),
                "exercises_to_avoid": profile.get('exercises_to_avoid'),
                "bmi": profile.get('bmi'),
                "just_viewed_profile": False,
                "reminders": [],
                "last_goal_check": datetime.now()
            }
            
            print(f"✅ Restored completed profile from DB for {sender}")
            
        else:
            # ✅ New user - start fresh onboarding
            user_sessions[sender] = {
                "messages": [],
                "onboarding_step": "basic",
                "name": None,
                "age": None,
                "gender": None,
                "weight": None,
                "height": None,
                "fitness_goal": None,
                "injury": None,
                "reminders": [],
                "last_goal_check": datetime.now(),
                "user_restrictions": None,
                "just_viewed_profile": False,
                "medical_conditions": None,
                "injuries": None,
                "allergies": None,
                "diet_preference": None,
                "activity_level": None,
                "stress_level": None,
                "workout_duration": None,
                "workout_location": None,
                "workout_time": None,
                "exercises_to_avoid": None,
                "bmi": None,
                "profile_completed": False,
                "profile_confirmed": False
            }

            # Greeting message for new users
            combined_intro = (
                "💪 Hey there! I'm *NexiFit*\n\n"
                "I'll help you stay consistent with workouts and food — right here on WhatsApp!\n\n"
                "Before we plan anything, tell me a bit about you.\n\n"
                "👉 *Name , Age , Gender*\n\n"
                "Example: Nexi , 27 , Male"
            )

            resp = MessagingResponse()
            resp.message(combined_intro)
            print(f"✅ New authorized user greeted: {sender}")
            return str(resp)

    session = user_sessions[sender]

    # Step 1: Basic Info
    if session["onboarding_step"] == "basic":
        # Extract info using LLM
        extracted = extract_user_info_from_text(incoming_msg)
        
        # Update session with whatever we got
        success = update_session_from_extracted_data(session, extracted, "basic")
        
        if success:
            # All basic info collected
            session["onboarding_step"] = "personalize"
            resp = MessagingResponse()
            resp.message(
                f"✅ Got it!\n"
                f"- Name: {session['name']}\n"
                f"- Age: {session['age']}\n"
                f"- Gender: {session['gender']}\n\n"
                "👉 Let’s make this personal\n\n"
                "Just tell me:\n"
                "- Weight\n"  
                "- Height\n" 
                "- Your goal (fat loss / muscle gain / overall fitness)\n\n"
                "If you want to keep it light for now, just type Skip — we can fine-tune anytime you want."
            )
            return str(resp)
        else:
            # Missing fields - ask again
            missing, error_msg = get_missing_fields(extracted, "basic")
            resp = MessagingResponse()
            resp.message(error_msg)
            return str(resp)

    # Step 2: Body Metrics (weight, height, goal)
    if session["onboarding_step"] == "personalize":
        
        if incoming_msg.lower().strip() == "skip":
            session["weight"] = session.get("weight") or "70kg"
            session["height"] = session.get("height") or "5'8\""
            session["fitness_goal"] = session.get("fitness_goal") or "overall fitness"
            session["onboarding_step"] = "health"
            resp = MessagingResponse()
            resp.message(
                "⏭️ Skipped! Let's talk about your health.\n\n"
                "🏥 Do you have any:\n"
                "1. Medical conditions (PCOD, diabetes, thyroid, etc.)\n"
                "2. Physical injuries or limitations (knee pain, back pain, etc.)\n"
                "3. Food allergies (dairy, nuts, gluten, etc.)\n\n"
                "Examples:\n"
                "• 'I have PCOD and weak knees, allergic to dairy'\n"
                "• 'No medical issues'\n"
                "•  Reply in your own words — or type 'Skip' if there is nothing to note."
            )
            return str(resp)
        
        # Extract body metrics
        extracted = extract_user_info_from_text(incoming_msg)
        
        # Update session
        if extracted.get("weight"):
            session["weight"] = extracted["weight"]
        if extracted.get("height"):
            session["height"] = extracted["height"]
        if extracted.get("fitness_goal"):
            session["fitness_goal"] = extracted["fitness_goal"]
        
        # Check if all required fields filled
        if session["weight"] and session["height"] and session["fitness_goal"]:
            # Calculate BMI
            bmi = calculate_bmi(session["weight"], session["height"])
            session["bmi"] = bmi
            
            bmi_msg = f" (BMI: {bmi} - {get_bmi_category(bmi)})" if bmi else ""
            
            session["onboarding_step"] = "health"
            resp = MessagingResponse()
            resp.message(
                f"✅ Perfect — thanks for sharing!\n"
                f"• Weight: {session['weight']}\n"
                f"• Height: {session['height']}{bmi_msg}\n"
                f"• Goal: {session['fitness_goal']}\n\n"
                "🏥 Before we move ahead, I just want to make sure I keep things safe and realistic for you.\n\n"
                "Let me know if you have any:\n"
                "1. Medical conditions (PCOD, diabetes, thyroid, etc.)\n"
                "2. Physical injuries (knee pain, back pain, etc.)\n"
                "3. Food allergies (dairy, nuts, gluten, etc.)\n\n"
                "Reply in your own words — or type Skip if there is nothing to note.\n"
            )
            return str(resp)
        else:
            # Missing fields
            missing = []
            if not session["weight"]:
                missing.append("weight")
            if not session["height"]:
                missing.append("height")
            if not session["fitness_goal"]:
                missing.append("fitness goal")
            
            resp = MessagingResponse()
            resp.message(
                f"⚠️ I couldn't extract: {', '.join(missing)}\n\n"
                "Please provide: Weight, Height, Fitness Goal\n\n"
                "Example: '75kg, 5'10\", build muscle'"
            )
            return str(resp)


    # Step 3: Health Info (medical conditions, injuries, allergies)
    if session["onboarding_step"] == "health":
        
        if incoming_msg.lower().strip() == "skip":
            session["medical_conditions"] = "None"
            session["injuries"] = "None"
            session["allergies"] = "None"
            session["onboarding_step"] = "lifestyle"
            
            resp = MessagingResponse()
            resp.message(
                "✅ Skipped health section.\n\n"
                "🍽️ Let's understand your lifestyle:\n\n"
                "Please share:\n"
                "1. Diet preference (Veg/Non-veg/Vegan)\n"
                "2. Daily activity level (Sitting/Moderate/Active)\n"
                "3. Current stress level (Low/Medium/High)\n\n"
                "Example: 'Vegetarian, mostly sitting, medium stress'"
            )
            return str(resp)
        
        # Extract health info
        health_data = extract_health_info(incoming_msg, llm)
        
        # Update session
        session["medical_conditions"] = health_data.get("medical_conditions") or "None"
        session["injuries"] = health_data.get("injuries") or "None"
        session["allergies"] = health_data.get("allergies") or "None"
        
        session["onboarding_step"] = "lifestyle"
        resp = MessagingResponse()
        
        health_summary = []
        if session.get("medical_conditions") != "None":
            health_summary.append(f"Medical: {session['medical_conditions']}")
        if session.get("injuries") != "None":
            health_summary.append(f"Injuries: {session['injuries']}")
        if session.get("allergies") != "None":
            health_summary.append(f"Allergies: {session['allergies']}")
        
        if health_summary:
            summary_text = "\n".join([f"• {item}" for item in health_summary])
            confirmation = f"✅ Got it!\n{summary_text}\n\n"
        else:
            confirmation = "✅ No health concerns noted.\n\n"
        
        resp.message(
            confirmation +
            "🍽️ Let's understand your lifestyle:\n\n"
            "Please share:\n"
            "1. Diet preference (Veg/Non-veg/Vegan/Eggetarian)\n"
            "2. Daily activity level outside workout\n"
            "3. Current stress level\n\n"
            "Example: 'Vegetarian, mostly sitting at desk, medium stress'"
        )
        return str(resp)


    # Step 4: Lifestyle (diet, activity, stress)
    if session["onboarding_step"] == "lifestyle":
        
        if incoming_msg.lower().strip() == "skip":
            # User skipped lifestyle - use defaults
            session["diet_preference"] = session.get("diet_preference") or "Mixed"
            session["activity_level"] = session.get("activity_level") or "Moderately Active"
            session["stress_level"] = session.get("stress_level") or "Moderate"
            session["onboarding_step"] = "workout_prefs"
            
            resp = MessagingResponse()
            resp.message(
                "✅ Skipped! Now for workout preferences:\n\n"
                "🏋️ Tell me:\n"
                "1. How much time for workouts\n"
                "2. Where you workout (Home/Gym)\n"
                "3. What time you prefer (optional)\n"
                "4. Any exercises to avoid (optional)\n\n"
                "Example: '30 minutes, home, evening'\n\n"
                "Or type 'Skip' to create your plan!"
            )
            return str(resp)
        
        # Extract lifestyle info
        lifestyle_data = extract_lifestyle_info(incoming_msg, llm)
        
        # Update session
        if lifestyle_data.get("diet_preference"):
            session["diet_preference"] = lifestyle_data["diet_preference"]
        if lifestyle_data.get("activity_level"):
            session["activity_level"] = lifestyle_data["activity_level"]
        if lifestyle_data.get("stress_level"):
            session["stress_level"] = lifestyle_data["stress_level"]
        
        # Check required fields
        if session.get("diet_preference") and session.get("activity_level") and session.get("stress_level"):
            session["onboarding_step"] = "workout_prefs"
            resp = MessagingResponse()
            resp.message(
                f"✅ Perfect!\n"
                f"• Diet: {session['diet_preference']}\n"
                f"• Activity: {session['activity_level']}\n"
                f"• Stress: {session['stress_level']}\n\n"
                "🏋️ Almost done! Workout preferences:\n\n"
                "Tell me:\n"
                "1. How much time you have for workouts\n"
                "2. Where you prefer to workout\n"
                "3. What time you usually workout\n"
                "4. Any exercises to avoid (optional)\n\n"
                "Example: '30 minutes, home workouts, evening 6pm, avoid burpees'"
            )
            return str(resp)
        else:
            missing = []
            if not session["diet_preference"]:
                missing.append("diet preference")
            if not session["activity_level"]:
                missing.append("activity level")
            if not session["stress_level"]:
                missing.append("stress level")
            
            resp = MessagingResponse()
            resp.message(
                f"⚠️ I couldn't extract: {', '.join(missing)}\n\n"
                "Please provide: Diet, Activity level, Stress level\n\n"
                "Example: 'Vegetarian, mostly sitting, medium stress'"
            )
            return str(resp)


    # Step 5: Workout Preferences
    if session["onboarding_step"] == "workout_prefs":

        # Check if user wants to skip to generate plan
        if incoming_msg.lower().strip() == "skip":
            # Use defaults for skipped fields
            session["workout_duration"] = session.get("workout_duration") or "30 minutes"
            session["workout_location"] = session.get("workout_location") or "Home"
            session["workout_time"] = session.get("workout_time") or "Flexible"
            session["exercises_to_avoid"] = session.get("exercises_to_avoid") or "None"
            
            # Generate and send general plan
            resp = MessagingResponse()
            resp.message("✅ Got it! Generating your personalized plan... 💪")
            
            # Start plan generation in background
            threading.Thread(target=handle_skip_and_generate_plan, args=(sender, session)).start()
            return str(resp)
            
        # Extract workout preferences
        workout_data = extract_workout_prefs(incoming_msg, llm)
        
        # Update session
        if workout_data.get("workout_duration"):
            session["workout_duration"] = workout_data["workout_duration"]
        if workout_data.get("workout_location"):
            session["workout_location"] = workout_data["workout_location"]
        if workout_data.get("workout_time"):
            session["workout_time"] = workout_data["workout_time"]
        
        # Handle exercises to avoid
        session["exercises_to_avoid"] = workout_data.get("exercises_to_avoid") or "None"
        
        # ✅ FIX: Allow flexible/no preferred time
        # If workout_time is None or empty, default to "Flexible"
        if not session.get("workout_time"):
            session["workout_time"] = "Flexible"
        
        # Check required fields (duration and location are mandatory, time can be flexible)
        if session.get("workout_duration") and session.get("workout_location"):
            
            # Validate critical fields before saving
            missing_critical = []
            if not session.get('weight'):
                missing_critical.append('weight')
            if not session.get('height'):
                missing_critical.append('height')
            if not session.get('fitness_goal'):
                missing_critical.append('fitness goal')
            
            if missing_critical:
                # Go back to collect missing fields
                resp = MessagingResponse()
                resp.message(
                    f"⚠️ Before we finish, I need your {', '.join(missing_critical)}!\n\n"
                    f"Please provide:\n"
                    f"• Weight (e.g., 75kg)\n"
                    f"• Height (e.g., 5'10\")\n"
                    f"• Fitness Goal (e.g., build muscle)\n\n"
                    f"Example: '75kg, 5'10\", build muscle'"
                )
                session["onboarding_step"] = "personalize"
                return str(resp)
            
            # Save complete profile to database
            profile_data = {
                'name': session['name'],
                'age': session['age'],
                'gender': session['gender'],
                'weight': session['weight'],
                'height': session['height'],
                'bmi': session.get('bmi'),
                'fitness_goal': session['fitness_goal'],
                'medical_conditions': session.get('medical_conditions'),
                'injuries': session.get('injuries'),
                'allergies': session.get('allergies'),
                'diet_preference': session['diet_preference'],
                'activity_level': session['activity_level'],
                'stress_level': session['stress_level'],
                'workout_duration': session['workout_duration'],
                'workout_location': session['workout_location'],
                'workout_time': session['workout_time'],  # Now guaranteed to have value
                'exercises_to_avoid': session.get('exercises_to_avoid'),
                'profile_completed': 1
            }
            
            save_user_profile(sender, profile_data)
            session["onboarding_step"] = "done"
            session["profile_completed"] = True
            session["profile_confirmed"] = False
    
            # Show complete profile summary
            bmi_text = ""
            if profile_data.get('bmi'):
                from database_pg import get_bmi_category
                category = get_bmi_category(profile_data['bmi'])
                bmi_text = f"BMI: {profile_data['bmi']} ({category})\n"
    
            summary = "📋 PROFILE SUMMARY\n\n"
    
            # Basic Info
            summary += "👤 BASIC INFO\n"
            summary += f"Name: {profile_data.get('name', 'N/A')}\n"
            summary += f"Age: {profile_data.get('age', 'N/A')}\n"
            summary += f"Gender: {profile_data.get('gender', 'N/A')}\n\n"
    
            # Body Metrics
            summary += "📊 BODY METRICS\n"
            summary += f"Weight: {profile_data.get('weight', 'N/A')}\n"
            summary += f"Height: {profile_data.get('height', 'N/A')}\n"
            if bmi_text:
                summary += bmi_text
            summary += f"Goal: {profile_data.get('fitness_goal', 'N/A')}\n\n"
    
            # Health Info
            has_health = (
                (profile_data.get('medical_conditions') and profile_data.get('medical_conditions') != 'None') or
                (profile_data.get('injuries') and profile_data.get('injuries') != 'None') or
                (profile_data.get('allergies') and profile_data.get('allergies') != 'None')
            )
    
            if has_health:
                summary += "🏥 HEALTH INFO\n"
                if profile_data.get('medical_conditions') and profile_data.get('medical_conditions') != 'None':
                    summary += f"Medical: {profile_data['medical_conditions']}\n"
                if profile_data.get('injuries') and profile_data.get('injuries') != 'None':
                    summary += f"Injuries: {profile_data['injuries']}\n"
                if profile_data.get('allergies') and profile_data.get('allergies') != 'None':
                    summary += f"Allergies: {profile_data['allergies']}\n"
                summary += "\n"
    
            # Lifestyle
            summary += "🍽️ LIFESTYLE\n"
            summary += f"Diet: {profile_data.get('diet_preference', 'N/A')}\n"
            summary += f"Activity: {profile_data.get('activity_level', 'N/A')}\n"
            summary += f"Stress: {profile_data.get('stress_level', 'N/A')}\n\n"
    
            # Workout Prefs
            summary += "🏋️ WORKOUT PREFS\n"
            summary += f"Duration: {profile_data.get('workout_duration', 'N/A')}\n"
            summary += f"Location: {profile_data.get('workout_location', 'N/A')}\n"
            summary += f"Time: {profile_data.get('workout_time', 'N/A')}\n"
            if profile_data.get('exercises_to_avoid') and profile_data.get('exercises_to_avoid') != 'None':
                summary += f"Avoid: {profile_data['exercises_to_avoid']}\n"
    
            resp = MessagingResponse()
            resp.message(
                f"{summary}\n\n"
                "Is this correct? Reply 'Yes' to continue or tell me what to change."
            )
            return str(resp)
        else:
            # Missing duration or location (time is now optional)
            missing = []
            if not session["workout_duration"]:
                missing.append("duration")
            if not session["workout_location"]:
                missing.append("location")
            
            resp = MessagingResponse()
            resp.message(
                f"⚠️ I couldn't extract: {', '.join(missing)}\n\n"
                "Please provide: Duration and Location\n"
                "(Time preference is optional - say 'flexible' if you don't have a preference)\n\n"
                "Example: '30 minutes, home, flexible'"
            )
            return str(resp)

            from database_pg import save_workout_schedule
            
            workout_time = session.get("workout_time", "Morning")
            success, normalized_time = save_workout_schedule(sender, workout_time)
            
            if success:
                print(f"✅ Workout schedule saved: {normalized_time}")
            else:
                print(f"⚠️ Failed to save workout schedule")
            
            # Save complete profile to database
            profile_data = {
                'name': session['name'],
                # ... all existing profile fields ...
            }
            
            save_user_profile(sender, profile_data)
            session["onboarding_step"] = "done"
            session["profile_completed"] = True
            session["profile_confirmed"] = False
            
            # ... existing profile summary code ...
            
            # ✅ UPDATE: Show scheduled time in summary
            resp = MessagingResponse()
            resp.message(
                f"{summary}\n\n"
                f"⏰ *Daily Workout Time:* {normalized_time or workout_time}\n"
                f"(You'll get automated plans at this time daily)\n\n"
                "Is this correct? Reply 'Yes' to continue or tell me what to change."
            )
            return str(resp)


    # Step 3: Normal conversation
    if session["onboarding_step"] == "done":
        
        msg_lower = incoming_msg.lower().strip()

        if any(keyword in msg_lower for keyword in ["remind", "set reminder", "alert", "alarm", "schedule", "notification"]):
            # Parse with LLM + fallback to regex
            task, run_time = parse_reminder_message(incoming_msg, llm)
            
            if task and run_time:
                # Successfully parsed reminder
                success = schedule_reminder(sender, task, run_time)
                
                if success:
                    # Calculate time difference for user-friendly display
                    now_ist = datetime.now(IST)
                    time_diff = run_time - now_ist
                    total_seconds = int(time_diff.total_seconds())
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    
                    if hours > 0:
                        time_str = f"in {hours}h {minutes}m"
                    else:
                        time_str = f"in {minutes} minutes"
                    
                    resp = MessagingResponse()
                    resp.message(
                        f"✅ Reminder set!\n\n"
                        f"📝 Task: {task}\n"
                        f"⏰ Time: {time_str}\n"
                        f"🕐 {run_time.strftime('%I:%M %p IST')}"
                    )
                    return str(resp)
                else:
                    resp = MessagingResponse()
                    resp.message("❌ Failed to set reminder. Please try again.")
                    return str(resp)
            else:
                # LLM couldn't parse - show examples
                resp = MessagingResponse()
                resp.message(
                    "⏰ I couldn't parse your reminder. Try being more specific!\n\n"
                    "Examples that work:\n"
                    "   • Remind me to drink water in 30 minutes\n"
                    "   • Set a reminder at 3:30pm for workout\n"
                    "   • I want a reminder about gym at 5pm\n"
                    "   • Remind me tomorrow at 8am about breakfast\n"
                    "   • Alert me in 2 hours to take medication\n"
                    "   • Schedule a reminder for lunch in 1 hour"
                )
                return str(resp)
        
        # ========== PROFILE CONFIRMATION HANDLER ==========
        if not session.get("profile_confirmed") and session.get("profile_completed"):
            # Track confirmation attempts
            if "confirmation_attempts" not in session:
                session["confirmation_attempts"] = 0
            
            if msg_lower in ['yes', 'yeah', 'yep', 'correct', 'right', 'ok', 'okay', 'confirm']:
                session["profile_confirmed"] = True
                session["confirmation_attempts"] = 0  # Reset
                
                # ✅ FIX: Validate critical fields before proceeding
                missing = []
                if not session.get('weight'):
                    missing.append('weight')
                if not session.get('height'):
                    missing.append('height')
                if not session.get('fitness_goal'):
                    missing.append('fitness goal')
                
                if missing:
                    resp = MessagingResponse()
                    resp.message(
                        f"⚠️ I need your {', '.join(missing)} first!\n\n"
                        f"Example: '75kg, 5'10\", build muscle'"
                    )
                    session["onboarding_step"] = "personalize"
                    session["profile_confirmed"] = False
                    return str(resp)
                
                # Mark profile as completed in database
                mark_profile_completed(sender)
                
                profile = get_user_profile(sender)
                
                scheduled_time = None
                if profile and profile.get('workout_time'):
                    from database_pg import normalize_workout_time
                    normalized_time = normalize_workout_time(profile['workout_time'])
                    
                    if normalized_time:
                        if schedule_user_daily_workout(sender, normalized_time):
                            scheduled_time = normalized_time
                            print(f"✅ Daily workouts scheduled for {sender} at {normalized_time}")
                        else:
                            print(f"⚠️ Failed to schedule daily workouts for {sender}")
                
                # Send confirmation message
                resp = MessagingResponse()
                
                confirmation_msg = f"🎯 Perfect, {session['name']}!\n\n"
                confirmation_msg += f"✅ Profile saved successfully\n"
                
                if scheduled_time:
                    confirmation_msg += f"✅ Daily workouts scheduled at {scheduled_time} IST\n"
                    confirmation_msg += f"   (Starting from tomorrow)\n\n"
                
                confirmation_msg += f"Creating your first workout plan now... 💪"
                
                resp.message(confirmation_msg)
                
                # ✅ Generate IMMEDIATE first plan (in background)
                session["messages"].append(HumanMessage(
                    content=f"Create my first personalized workout plan for today based on my profile."
                ))
                
                threading.Thread(target=process_and_reply, args=(sender, True, "")).start()
                return str(resp)
                
                # ✅ FIX: Add the message to history and generate plan
                session["messages"].append(HumanMessage(
                    content=f"Create my first personalized workout plan for today based on my profile."
                ))
                
                # Start plan generation in background
                threading.Thread(target=process_and_reply, args=(sender, True, incoming_msg)).start()
                return str(resp)
                
            elif msg_lower in ['no', 'nope', 'change', 'incorrect', 'wrong']:
                session["confirmation_attempts"] = 0  # Reset
                resp = MessagingResponse()
                resp.message(
                    "📝 What would you like to change?\n\n"
                    "Examples:\n"
                    "• 'Change my weight to 75kg'\n"
                    "• 'Update goal to lose weight'\n"
                    "• 'My diet is vegetarian'"
                )
                session["profile_confirmed"] = True  # Allow them to proceed after changes
                return str(resp)
                
            else:
                session["confirmation_attempts"] += 1
                
                # After 3 attempts, auto-confirm and move on
                if session["confirmation_attempts"] >= 3:
                    session["profile_confirmed"] = True
                    session["confirmation_attempts"] = 0  # Reset
                    mark_profile_completed(sender)
                    
                    resp = MessagingResponse()
                    resp.message(
                        "✅ I'll use your profile as-is.\n\n"
                        "You can always update it later by saying 'change my [field]'.\n\n"
                        "Creating your first plan... 💪"
                    )
                    
                    # ✅ FIX: Generate plan after auto-confirmation too
                    session["messages"].append(HumanMessage(
                        content=f"Create my first personalized workout plan for today based on my profile."
                    ))
                    
                    threading.Thread(target=process_and_reply, args=(sender, True, incoming_msg)).start()
                    return str(resp)
                
                # Still trying to confirm
                resp = MessagingResponse()
                resp.message(
                    "Please confirm your profile:\n\n"
                    "Reply:\n"
                    "• 'Yes' to confirm\n"
                    "• 'No' to make changes"
                )
                return str(resp)
                
        # ========== END CONFIRMATION HANDLER ==========

        # Check if user wants to view profile
        if msg_lower in ['profile', 'my profile', 'show profile', 'view profile', 'current profile']:
            session["just_viewed_profile"] = True
            resp = MessagingResponse()
            resp.message(get_current_profile(session))
            return str(resp)
        
        # Check if user wants to update profile
        if is_update_request(incoming_msg):
            # Extract what they want to update
            update_data = extract_update_fields(incoming_msg)
            
            # Update session with new values
            confirmation = update_user_profile(session, update_data)
            
            resp = MessagingResponse()
            resp.message(confirmation)
            return str(resp)
        
        # Streak tracking functions
        msg_lower = incoming_msg.lower().strip()
        
        if msg_lower in ['streak', 'my streak', 'check streak', 'show streak', 'streak stats']:
            streak_data = get_user_streak(sender)
            current = streak_data['current_streak']
            longest = streak_data['longest_streak']
            
            if current == 0:
                message = (
                    "📊 *Your Workout Streak*\n\n"
                    "You haven't started your streak yet!\n"
                    "Complete a workout today to begin! 💪"
                )
            else:
                # Choose emoji based on streak
                if current >= 7:
                    emoji = "🔥"
                elif current >= 3:
                    emoji = "💪"
                else:
                    emoji = "✨"
                
                message = (
                    f"{emoji} *Your Workout Streak*\n\n"
                    f"Current Streak: *{current} days* 🎯\n"
                    f"Longest Streak: *{longest} days* 🏆\n\n"
                )
                
                # Add encouraging message
                if current == longest and current >= 3:
                    message += "You're at your personal best! 🚀"
                elif current >= 7:
                    message += "Amazing consistency! Keep going! 💪"
                else:
                    message += "Keep building that momentum! 💪"
            
            resp = MessagingResponse()
            resp.message(message)
            return str(resp)

        # ===== HANDLE TIP OPT-OUT/OPT-IN =====
        msg_lower = incoming_msg.lower().strip()
        
        if msg_lower in ['stop tips', 'no tips', 'disable tips', 'unsubscribe tips']:
            set_user_tip_preference(sender, False)
            resp = MessagingResponse()
            resp.message(
                "✅ You've been unsubscribed from daily mental health tips.\n\n"
                "You can re-enable them anytime by sending 'START TIPS'."
            )
            return str(resp)
        
        if msg_lower in ['start tips', 'enable tips', 'resume tips', 'subscribe tips']:
            set_user_tip_preference(sender, True)
            resp = MessagingResponse()
            resp.message(
                "✅ Daily mental health tips enabled!\n\n"
                "You'll receive a morning wellness tip every day at 7:00 AM. 🌅"
            )
            return str(resp)
        # ===== END TIP HANDLING =====
        

        # Handle weekly/daily plan requests
        msg_lower = incoming_msg.lower()
        if any(word in msg_lower for word in ["weekly plan", "week plan", "7 day", "full week", "weekly routine", "weekly workout"]):
            session["messages"].append(HumanMessage(
                content=f"Create a complete weekly workout plan (Monday to Sunday) for me based on my goal: {session['fitness_goal']}. "
                        f"Include rest days and specify which muscle groups to target each day."
            ))
            resp = MessagingResponse()
            resp.message("📅 Creating your weekly workout plan...")
            threading.Thread(target=process_and_reply, args=(sender, True)).start()
            return str(resp)
        
        elif any(word in msg_lower for word in ["today", "today's plan", "workout for today"]):
            session["messages"].append(HumanMessage(
                content="What's my workout plan for today?"
            ))
            resp = MessagingResponse()
            resp.message("📋 Preparing today's workout plan...")
            threading.Thread(target=process_and_reply, args=(sender, True)).start()
            return str(resp)
        if session.get("just_viewed_profile"):
            session["just_viewed_profile"] = False  # Reset flag
        
            if msg_lower in ['no', 'nope', 'looks good', 'all good', 'correct', 'fine', 'ok', 'okay']:
                resp = MessagingResponse()
                resp.message(
                    "✅ Great! Your profile is all set.\n\n"
                    "What would you like help with today?\n\n"
                    "I can help you with:\n"
                    "   - Create workout plans\n"
                    "   - Design meal plans\n"
                    "   - Answer fitness questions\n"
                    "   - Track your progress\n\n"
                    "Just let me know! 💪"
                )
                return str(resp)
            
            elif msg_lower in ['yes', 'yeah', 'update', 'change', 'modify']:
                resp = MessagingResponse()
                resp.message(
                    "📝 What would you like to update?\n\n"
                    "Examples:\n"
                    "   - 'Change my weight to 75kg'\n"
                    "   - 'Update goal to lose weight'\n"
                    "   - 'My stress level is high now'\n\n"
                    "Just tell me what to change!"
                )
                return str(resp)

        # IMPROVED: More lenient fitness topic check
        if not is_fitness_related(incoming_msg):
            resp = MessagingResponse()
            resp.message(
                "⚠️ I specialize in fitness topics like workouts, diet, nutrition, and exercise.\n\n"
                "Feel free to ask me anything about your fitness journey! 💪"
            )
            return str(resp)

        # Add message to history and process
        session["messages"].append(HumanMessage(content=incoming_msg))
        print(f"💬 Processing message. History length: {len(session['messages'])}")

        resp = MessagingResponse()
        resp.message("✅ Got it! Let me help you with that...")
        
        # Determine if this is an initial plan request
        msg_lower = incoming_msg.lower()
        is_plan_request = any(word in msg_lower for word in ["plan", "workout", "today", "weekly", "routine", "suggest"])

        # Start only ONE thread with the appropriate flag
        threading.Thread(target=process_and_reply, args=(sender, is_plan_request, incoming_msg)).start()
        return str(resp)
        

# -------------------------
# 🆕 USER PROFILE UPDATE FUNCTIONS
# -------------------------

def is_update_request(message):
    """
    Check if user wants to update their profile.
    
    Args:
        message (str): User's message
        
    Returns:
        bool: True if this is an update request
    """
    message_lower = message.lower().strip()
    
    update_keywords = [
        "change", "update", "modify", "edit", "new", "different",
        "my age is", "my weight is", "my height is", "my goal is",
        "i'm now", "i am now", "i weigh", "i'm weighing",
        "update my", "change my", "modify my", "edit my",
        "actually", "wait i meant", "i meant to say", "correction"
    ]
    
    return any(keyword in message_lower for keyword in update_keywords)


def extract_update_fields(message):
    """
    Extract which fields user wants to update from their message.
    
    Args:
        message (str): User's message
        
    Returns:
        dict: Contains extracted update fields with descriptions
    """
    try:
        # Use LLM to identify what's being updated
        update_prompt = f"""User wants to update their profile. Analyze this message and return JSON:

Message: "{message}"

Return ONLY valid JSON identifying what they want to update:
{{
  "fields_to_update": ["age", "weight", "height", "goal", "injury", "name"],  // list of fields they mention
  "age": value or null,
  "weight": value or null,
  "height": value or null,
  "fitness_goal": value or null,
  "injury": value or null,
  "name": value or null
}}

Examples:
- "my age is now 26" → {{"fields_to_update": ["age"], "age": "26"}}
- "i now weigh 80kg and my goal is lose fat" → {{"fields_to_update": ["weight", "fitness_goal"], "weight": "80kg", "fitness_goal": "lose weight"}}
- "update my height to 6 feet" → {{"fields_to_update": ["height"], "height": "6 feet"}}
"""
        
        response = llm.invoke([LangChainHumanMessage(content=update_prompt)])
        response_text = response.content.strip()
        
        # Clean response
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        update_data = json.loads(response_text)
        print(f"✅ Update request detected: {update_data}")
        return update_data
        
    except Exception as e:
        print(f"❌ Update extraction error: {e}")
        return {
            "fields_to_update": [],
            "age": None,
            "weight": None,
            "height": None,
            "fitness_goal": None,
            "injury": None,
            "name": None
        }


def update_user_profile(session, update_data):
    """
    Update user session with new profile information.
    
    Args:
        session (dict): User's session
        update_data (dict): Extracted update data
        
    Returns:
        str: Confirmation message of what was updated
    """
    fields_to_update = update_data.get("fields_to_update", [])
    updated_fields = []
    
    if not fields_to_update:
        return "⚠️ Couldn't identify which field to update. Please be more specific.\n\nExample: 'My weight is now 80kg' or 'Update my goal to lose fat'"
    
    # Update each field
    if "name" in fields_to_update and update_data.get("name"):
        session["name"] = update_data["name"]
        updated_fields.append(f"Name: {update_data['name']}")
    
    if "age" in fields_to_update and update_data.get("age"):
        session["age"] = update_data["age"]
        updated_fields.append(f"Age: {update_data['age']}")
    
    if "weight" in fields_to_update and update_data.get("weight"):
        session["weight"] = update_data["weight"]
        updated_fields.append(f"Weight: {update_data['weight']}")
    
    if "height" in fields_to_update and update_data.get("height"):
        session["height"] = update_data["height"]
        updated_fields.append(f"Height: {update_data['height']}")
    
    if "fitness_goal" in fields_to_update and update_data.get("fitness_goal"):
        session["fitness_goal"] = update_data["fitness_goal"]
        updated_fields.append(f"Goal: {update_data['fitness_goal']}")
    
    if "injury" in fields_to_update and update_data.get("injury"):
        session["injury"] = update_data["injury"]
        updated_fields.append(f"Injury/Restrictions: {update_data['injury']}")
    
    if not updated_fields:
        return "⚠️ Couldn't extract valid values to update. Please try again.\n\nExample: 'My weight is now 80kg'"
    
    # Build confirmation message
    confirmation = "✅ *Profile Updated!*\n\n"
    for field in updated_fields:
        confirmation += f"• {field}\n"
    
    confirmation += "\n💡 Your next plan will use these updated details!"
    
    return confirmation


def get_current_profile(session):
    """Enhanced profile with all fields."""
    
    # Calculate BMI
    bmi_text = ""
    if session.get('weight') and session.get('height'):
        from database_pg import calculate_bmi, get_bmi_category
        bmi = calculate_bmi(session['weight'], session['height'])
        if bmi:
            category = get_bmi_category(bmi)
            bmi_text = f"BMI: {bmi} ({category})\n"
    
    profile = "📋 PROFILE SUMMARY\n\n"
    
    # Basic Info
    profile += "👤 BASIC INFO\n"
    profile += f"Name: {session.get('name', 'N/A')}\n"
    profile += f"Age: {session.get('age', 'N/A')}\n"
    profile += f"Gender: {session.get('gender', 'N/A')}\n\n"
    
    # Body Metrics
    profile += "📊 BODY METRICS\n"
    profile += f"Weight: {session.get('weight', 'N/A')}\n"
    profile += f"Height: {session.get('height', 'N/A')}\n"
    if bmi_text:
        profile += bmi_text
    profile += f"Goal: {session.get('fitness_goal', 'N/A')}\n\n"
    
    # Health Info (if present)
    has_health = (
        (session.get('medical_conditions') and session.get('medical_conditions') != 'None') or
        (session.get('injuries') and session.get('injuries') != 'None') or
        (session.get('allergies') and session.get('allergies') != 'None')
    )
    
    if has_health:
        profile += "🏥 HEALTH INFO\n"
        if session.get('medical_conditions') and session.get('medical_conditions') != 'None':
            profile += f"Medical: {session['medical_conditions']}\n"
        if session.get('injuries') and session.get('injuries') != 'None':
            profile += f"Injuries: {session['injuries']}\n"
        if session.get('allergies') and session.get('allergies') != 'None':
            profile += f"Allergies: {session['allergies']}\n"
        profile += "\n"
    
    # Lifestyle (if profile completed)
    if session.get('diet_preference'):
        profile += "🍽️ LIFESTYLE\n"
        profile += f"Diet: {session.get('diet_preference', 'N/A')}\n"
        profile += f"Activity: {session.get('activity_level', 'N/A')}\n"
        profile += f"Stress: {session.get('stress_level', 'N/A')}\n\n"
    
    # Workout Prefs (if profile completed)
    if session.get('workout_duration'):
        profile += "🏋️ WORKOUT PREFS\n"
        profile += f"Duration: {session.get('workout_duration', 'N/A')}\n"
        profile += f"Location: {session.get('workout_location', 'N/A')}\n"
        profile += f"Time: {session.get('workout_time', 'N/A')}\n"
        if session.get('exercises_to_avoid') and session.get('exercises_to_avoid') != 'None':
            profile += f"Avoid: {session['exercises_to_avoid']}\n"
        profile += "\n"
    
    profile += "Want to update any of these? Just tell me!"
    
    return profile

import json
from langchain_core.messages import HumanMessage as LangChainHumanMessage

# -------------------------
# 🆕 SMART INPUT EXTRACTION WITH LLM
# -------------------------

extraction_system_prompt = """You are a data extraction assistant. Extract user information from free-form text.
Return ONLY valid JSON, no extra text.

Extract these fields if mentioned:
- name: Full name (string or null)
- age: Age as number (int or null)
- gender: Male/Female/Other (string or null)
- weight: Weight AS STRING - just extract the number and any unit if present (string or null)
- height: Height AS STRING - just extract the number and any unit/format if present (string or null)
- fitness_goal: Goal like "build muscle", "lose weight", "increase stamina" (string or null)
- injury: Any injuries or restrictions (string or null)
- time_restriction: Time available like "30 minutes", "1 hour" (string or null)

WEIGHT EXTRACTION RULES:
- "75" → "75" (preserve as-is, we assume kg later)
- "75kg" → "75kg" (preserve unit)
- "165lbs" → "165lbs" (preserve unit)
- "165 pounds" → "165 pounds" (preserve unit)
- "75 kgs" → "75kg" (normalize)
- Extract ONLY the number portion, preserve exact unit if given
- DO NOT add units if user didn't provide them

HEIGHT EXTRACTION RULES:
- "180" → "180" (preserve as-is)
- "180cm" → "180cm" (preserve unit)
- "5'10"" → "5'10"" (preserve format)
- "5 10" → "5 10" (preserve format)
- "1.8m" → "1.8m" (preserve unit)
- "5.8" → "5.8" (ambiguous, preserve)
- Extract EXACTLY as user wrote it
- DO NOT add units if user didn't provide them

Be lenient with variations:
- "wanna bulk" → fitness_goal: "build muscle"
- "bad knee" → injury: "knee pain"
- "30 mins" → time_restriction: "30 minutes"
- "im 75 kilos" → weight: "75"
- "i weigh 70" → weight: "70"
- "my height is 180" → height: "180"
- "75, 180, muscle" → weight: "75", height: "180", fitness_goal: "build muscle"

Return format:
{
  "name": value or null,
  "age": value or null,
  "gender": value or null,
  "weight": value or null,
  "height": value or null,
  "fitness_goal": value or null,
  "injury": value or null,
  "time_restriction": value or null
}
"""

def extract_user_info_from_text(user_message):
    """
    Extract structured user information from free-form text using LLM.
    
    Args:
        user_message (str): User's input in any format
        
    Returns:
        dict: Extracted information with keys: name, age, gender, weight, height, fitness_goal, injury, time_restriction
        dict with None values for missing fields
    """
    try:
        # Create extraction prompt
        extraction_prompt = f"""Extract user information from this message:
"{user_message}"

Return ONLY valid JSON with no additional text or markdown."""
        
        # Call Gemini for extraction
        messages = [
            LangChainHumanMessage(content=extraction_system_prompt),
            LangChainHumanMessage(content=extraction_prompt)
        ]
        
        response = llm.invoke(messages)
        response_text = response.content.strip()
        
        # Clean response (remove markdown code blocks if present)
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        # Parse JSON
        extracted_data = json.loads(response_text)
        
        # Ensure all expected keys exist
        default_keys = {
            "name": None,
            "age": None,
            "gender": None,
            "weight": None,
            "height": None,
            "fitness_goal": None,
            "injury": None,
            "time_restriction": None
        }
        
        # Merge with defaults
        result = {**default_keys, **extracted_data}
        
        print(f"✅ Extracted data: {result}")
        return result
        
    except json.JSONDecodeError as e:
        print(f"❌ JSON Parse Error: {e}")
        return {
            "name": None,
            "age": None,
            "gender": None,
            "weight": None,
            "height": None,
            "fitness_goal": None,
            "injury": None,
            "time_restriction": None
        }
    except Exception as e:
        print(f"❌ Extraction Error: {e}")
        return {
            "name": None,
            "age": None,
            "gender": None,
            "weight": None,
            "height": None,
            "fitness_goal": None,
            "injury": None,
            "time_restriction": None
        }


def get_missing_fields(extracted_data, step):
    """
    Determine which fields are missing based on onboarding step.
    
    Args:
        extracted_data (dict): Data extracted from user message
        step (str): Current onboarding step ("basic", "restrictions", "personalize")
        
    Returns:
        list: List of missing field names
        str: Human-readable message asking for missing fields
    """
    missing = []
    
    if step == "basic":
        # Basic step requires: name, age, gender
        if not extracted_data.get("name"):
            missing.append("name")
        if not extracted_data.get("age"):
            missing.append("age")
        if not extracted_data.get("gender"):
            missing.append("gender")
        
        if missing:
            msg = f"⚠️ I couldn't extract {', '.join(missing)}.\n\n" \
                  f"Please provide: Name, Age, Gender\n\n" \
                  f"Example: John, 25, Male"
            return missing, msg
        
        return [], None
    
    elif step == "restrictions":
        # Restrictions step: optional but useful
        # We can accept time_restriction or injury
        # If user says "no" they skip, so this is flexible
        return [], None
    
    elif step == "personalize":
        # Personalize step: weight, height, fitness_goal, injury
        if not extracted_data.get("weight"):
            missing.append("weight")
        if not extracted_data.get("height"):
            missing.append("height")
        if not extracted_data.get("fitness_goal"):
            missing.append("fitness_goal")
        # injury is optional
        
        if missing:
            msg = f"⚠️ I couldn't extract {', '.join(missing)}.\n\n" \
                  f"Please provide: Weight, Height, Fitness Goal (and injury if applicable)\n\n" \
                  f"Example: 75kg, 5'10\", Build Muscle, Knee pain"
            return missing, msg
        
        return [], None
    
    return [], None


def update_session_from_extracted_data(session, extracted_data, step):
    """
    Update user session with extracted information.
    
    Args:
        session (dict): User session dictionary
        extracted_data (dict): Data extracted from LLM
        step (str): Current onboarding step
        
    Returns:
        bool: True if update successful, False if missing required fields
    """
    
    if step == "basic":
        if extracted_data.get("name"):
            session["name"] = extracted_data["name"]
        if extracted_data.get("age"):
            session["age"] = extracted_data["age"]
        if extracted_data.get("gender"):
            session["gender"] = extracted_data["gender"]
        
        # Check if all basic fields are filled
        if session["name"] and session["age"] and session["gender"]:
            return True
        return False
    
    elif step == "restrictions":
        # Update restrictions if provided
        if extracted_data.get("time_restriction"):
            session["user_restrictions"] = extracted_data["time_restriction"]
        elif extracted_data.get("injury"):
            session["user_restrictions"] = extracted_data["injury"]
        else:
            session["user_restrictions"] = extracted_data.get("injury") or "None"
        
        # Restrictions step always moves forward
        return True
    
    elif step == "personalize":
        if extracted_data.get("weight"):
            session["weight"] = extracted_data["weight"]
        if extracted_data.get("height"):
            session["height"] = extracted_data["height"]
        if extracted_data.get("fitness_goal"):
            session["fitness_goal"] = extracted_data["fitness_goal"]
        if extracted_data.get("injury"):
            session["injury"] = extracted_data["injury"]
        
        # Check if key personalization fields are filled
        if session["weight"] and session["height"] and session["fitness_goal"]:
            return True
        return False
    
    return False

@app.route("/health")
def health():
    return "OK", 200

print("🔧 Initializing scheduler...")
scheduler = init_scheduler()

if scheduler and scheduler.running:
    print(f"✅ Scheduler active: {len(scheduler.get_jobs())} jobs")
else:
    print("❌ Scheduler initialization failed!")
print("="*70 + "\n")

if __name__ == "__main__":
    print("="*70)
    print("🚀 Starting NexiFit application (Development Mode)")
    print("="*70)
    
    # Scheduler already initialized at module level
    if scheduler and scheduler.running:
        print(f"✅ Scheduler confirmed: {len(scheduler.get_jobs())} jobs scheduled")
    else:
        print("⚠️ WARNING: Scheduler not initialized!")
        scheduler = init_scheduler()
    
    print("🌐 Starting Flask server...")
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False
    )
