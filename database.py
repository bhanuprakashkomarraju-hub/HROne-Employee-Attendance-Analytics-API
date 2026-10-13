
import os
from pathlib import Path
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables from the .env file
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# Get MongoDB connection string
MONGODB_URI = os.getenv("MONGODB_URI")

if not MONGODB_URI:
    raise ValueError(
        "MONGODB_URI not found. Please check your .env file."
    )

# Create MongoDB client
client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000
)

# Select the HROne database
db = client["hrone"]


def check_connection():
    """Check whether MongoDB Atlas is reachable."""
    try:
        client.admin.command("ping")
        print("MongoDB Atlas connected successfully!")
        return True
    except Exception as error:
        print("MongoDB connection failed:", error)
        return False


if __name__ == "__main__":
    try:
        if check_connection():
            print("Database selected:", db.name)
    finally:
        client.close()


