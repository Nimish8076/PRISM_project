import anthropic
import os
import pandas as pd
from dotenv import load_dotenv
from config import CHAT_MODEL

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def generate_answer(user_question: str, sql_query: str, results_df: pd.DataFrame) -> str:
    """
    Takes the user question, SQL query, and results dataframe
    and returns a plain English answer.
    """

    if results_df is None or len(results_df) == 0:
        return "I ran the query but it returned no results. Try rephrasing your question or ask about a different metric."

    # Convert dataframe to readable string (limit to 50 rows for context)
    results_str = results_df.head(50).to_string(index=False)

    prompt = f"""
You are a friendly data analyst assistant for Jersey Mike's Subs franchise operations.

The user asked: "{user_question}"

The SQL query that ran:
{sql_query}

The results:
{results_str}

Now answer the user's question in clear, friendly, business-language.
- Be specific with numbers and store names
- Highlight the most important finding first
- If there are rankings, mention the top and bottom performers
- Keep it concise — 3 to 5 sentences maximum
- Don't mention SQL or technical details
- Use business terms like "same store sales", "OSAT score", "food safety score" etc.
"""

    try:
        response = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()

    except Exception as e:
        return f"I found the data but had trouble summarizing it. Here are the raw results above."
