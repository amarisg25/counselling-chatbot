from langchain.prompts import PromptTemplate
from dotenv import load_dotenv
import json
from langchain_community.document_loaders import DirectoryLoader, JSONLoader, WebBaseLoader
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_openai import ChatOpenAI
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains import RetrievalQA
from langchain import hub
import autogen
from langchain.tools import BaseTool, StructuredTool, Tool, tool
from autogen.agentchat.contrib.retrieve_user_proxy_agent import RetrieveUserProxyAgent
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import List, Union, Dict, Optional
import os
from autogen.agentchat.contrib.capabilities.teachability import Teachability
from .functions import search_provider, assess_ttm_stage_single_question, assess_hiv_risk, notify_research_assistant, record_support_request
import time
import hashlib
from typing import Set
import platform
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from bs4 import BeautifulSoup
import pandas as pd
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import shutil
import chromadb
from chromadb.config import Settings
import sqlite3
from supabase import create_client
import openai
from openai import OpenAI


# CONFIGURATION 
os.environ["TOKENIZERS_PARALLELISM"] = "false"




class TrackableGroupChatManager(autogen.GroupChatManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_message = None
        self._message_history = set()

    # def _process_received_message(self, message, sender, silent):
    #     if self.websocket:
    #         # formatted_message = self._format_message(message, sender)
    #         # if formatted_message and formatted_message not in self._message_history:  
    #         asyncio.create_task(self.send_message(message))
    #         self._message_history.add(message)
    #     return super()._process_received_message(message, sender, silent)

    def _format_message(self, message, sender) -> str:
        """Format the message for display, handling various message types"""
        try:
       
            if isinstance(message, dict):
                if 'function_call' in message or 'tool_calls' in message:
                    return None
                
                if 'content' in message and message['content']:
                    return self._clean_message(message['content'])
                
                if 'role' in message and message['role'] == 'tool':
                    if 'content' in message:
                        return self._clean_message(message['content'])
                    
            elif isinstance(message, str) and message.strip():
                return self._clean_message(message)
                
            return None
        except Exception as e:
            print(f"Error formatting message: {e}")
            return None

    def _clean_message(self, message: str) -> str:
        """Clean and format message content"""
        # Remove any existing prefixes
        prefixes = ["counselor:", "CHIA:", "assessment_bot:"]
        message = message.strip()

        for prefix in prefixes:
            if message.lower().startswith(prefix.lower()):
                message = message[len(prefix):].strip()
        return message

    async def send_message(self, message: str):
        """Send message to websocket, avoiding duplicates"""
        if message and message != self._last_message:
            try:
                await self.websocket.send_text(message)
                self._last_message = message
            except Exception as e:
                print(f"Error sending message: {e}")

    def _process_received_message(self, message, sender, silent):
        """Process and deduplicate messages"""
        if self.websocket:
            formatted_message = self._format_message(message, sender)
            if formatted_message:
                # message_hash = self._calculate_message_hash(formatted_message, sender.name)
                
                # if not self._is_duplicate(message_hash):
                #     self._message_timestamps[message_hash] = time.time()
                #     self._message_history.add(message_hash)
                asyncio.create_task(self.send_message(formatted_message))

        return super()._process_received_message(message, sender, silent)

    async def send_message(self, message: str):
        """Send message to websocket with deduplication"""
        try:
            print("sending message")
            if message and message != self._last_message:
                await self.websocket.send_text(message)
                self._last_message = message
        except Exception as e:
            print(f"Error sending message: {e}")


class HIVPrEPCounselor:
    def __init__(self, websocket: WebSocket, user_id: str, chat_id: str = None, teachability_flag: bool = None):
        load_dotenv()
        self.user_id = user_id
        self.chat_id = chat_id 
        self.teachability_flag = teachability_flag
        print(f"[INIT] Teachability flag set to: {self.teachability_flag}")
        print("chat_id", self.chat_id)
        self.api_key = os.getenv('OPENAI_API_KEY')
        self.websocket = websocket
        self._vectorstore = None  # For caching
        self._qa_chain = None  # For caching
        self.response_cache = {}  # Cache for common responses
        self.teachability = None  # Add this line

        if not self.api_key:
            raise ValueError("API key not found. Please set OPENAI_API_KEY in your .env file.")

        # Use less expensive models for non-critical tasks
        self.config_list = {
            "model": "ft:gpt-4o-2024-08-06:brown-university::B4YXCCUH",
            "api_key": self.api_key,
            "price": [1.25, 1.25]
        }

        # Use a more efficient embedding model
        self.embedding_model = "text-embedding-3-small"
        
        self.agent_history = []
        self.setup_rag()
        self.initialize_teachability()  # Move teachability initialization before agents
        self.initialize_agents()

        # Store teachability state
        self.teachability_flag = teachability_flag
        
        # Send initial teachability state to frontend
        asyncio.create_task(self.send_teachability_state())

    def check_termination(self, x):
        return x.get("content", "").rstrip().lower() == "end conversation"

    def setup_rag(self):
        # Check if we already have the vectorstore cached
        if self._vectorstore is None:
            prompt = PromptTemplate(
                template="""You are a knowledgeable HIV prevention counselor.
                - The priority is to use the context to answer the question. 
                - If the answer is in the context, make sure to only use all the information available in the context related to the question to answer the question. Do not make up any additional information.

                Context: {context}

                Question: {question}

                - Use "sex without condoms" instead of "unprotected sex"
                - Use "STI" instead of "STD"

                If the answer is not in the context, do not say "I don't know." Instead, provide helpful guidance based on what you do know but do not mention the answer is not in the context.
                If the answer is in the context, do not add information to the answer. You should use motivational interviewing techniques to answer the question.

                Answer: """,
                input_variables=["context", "question"]
            )

            try:
                # Try to load from disk first
                self._vectorstore = Chroma(
                    persist_directory="./chroma_db", 
                    embedding_function=OpenAIEmbeddings(
                        model=self.embedding_model,
                        openai_api_key=self.api_key
                    )
                )
                print("Loaded vectorstore from disk")
            except Exception:
                # If not available, create and persist
                loader = WebBaseLoader("https://github.com/amarisg25/embedding-data-chatbot/blob/main/HIV_PrEP_knowledge_embedding.json")
                data = loader.load()
                text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
                all_splits = text_splitter.split_documents(data)
                
                self._vectorstore = Chroma.from_documents(
                    documents=all_splits, 
                    embedding=OpenAIEmbeddings(
                        model=self.embedding_model,
                        openai_api_key=self.api_key
                    ),
                    persist_directory="./chroma_db"
                )
                self._vectorstore.persist()
                print("Created and persisted new vectorstore")


            llm = ChatOpenAI(model_name="ft:gpt-4o-2024-08-06:brown-university::B4YXCCUH", temperature=0)
            retriever = self._vectorstore.as_retriever(
                search_kwargs={"k": 3} 
            )
            
            self._qa_chain = RetrievalQA.from_chain_type(
                llm, retriever=retriever, chain_type_kwargs={"prompt": prompt}
            )

    def answer_question(self, question: str) -> str:
        # Check cache first
        cache_key = question.lower().strip()
        if cache_key in self.response_cache:
            print("Cache hit!")
            return self.response_cache[cache_key]
        
        # Not in cache, get from QA chain
        self.result = self._qa_chain.invoke({"query": question})
        answer = self.result.get("result", "I'm sorry, I couldn't find an answer to that question.")
        
        # Cache the result for future use (limit cache size)
        if len(self.response_cache) < 100:  # Prevent unlimited growth
            self.response_cache[cache_key] = answer
            
        return answer

    def initialize_teachability(self):
        """Initialize teachability components"""
        if self.teachability_flag:
            user_db_path = os.path.join(
                "./tmp/interactive/teachability_db",
                f"user_{self.user_id}"
            )
            os.makedirs(user_db_path, exist_ok=True)
            
            self.teachability = Teachability(
                reset_db=False,
                path_to_db_dir=user_db_path,
                recall_threshold=2.5,
                verbosity=1
            )
            print(f"Teachability initialized with path: {user_db_path}")
            print("Memo store contents:", self.teachability.memo_store)

    def initialize_agents(self):
        counselor_system_message = """You are CHIA, the primary HIV PrEP counselor.
        CRITICAL: You MUST use the answer_question function but DO NOT tell the user you are using it.
        Take your time to think about the answer but don't say anything to the user until you have the answer.
        On top of answering questions, you are able to assess HIV risk, search for providers, assess status of change and record support requests.

        Key Guidelines:
        1. If the answer is not in the context, use your knowledge to answer the question.

        2. Use motivational interviewing techniques to answer the question.

        3. YOU ARE THE PRIMARY RESPONDER. Always respond first unless:
        - User explicitly asks for risk assessment
        - User explicitly asks to find a provider

        4. For ANY HIV/PrEP questions:
        - Format response warmly and conversationally
        - Use "sex without condoms" instead of "unprotected sex"
        - Use "STI" instead of "STD"
        - If unsure about specific details, focus on connecting them with healthcare providers

        5. When user shares their name:
        - Thank them for chatting
        - Explain confidentiality

        6. If someone thinks they have HIV:
        - FIRST call answer_question to get accurate information
        - Then provide support and options for assessment/providers
        - Never leave them without resources or next steps

        7. Before answering a question:
        - Ensure the answer makes sense in conversation context
        - If uncertain, focus on connecting them with appropriate resources
        - Always provide a clear next step or action item

        8. For any other questions:
        - Answer as a counselor using motivational interviewing techniques
        - Focus on what you can do to help
        - Provide clear next steps
        - Only suggest the user to reach out to a healthcare provider who can offer personalized advice and support somtimes when necessary. BUT do not do it too often as it can be annoying.

        REMEMBER: 
        If the answer is unclear, focus on connecting them with healthcare providers who can help."""

        patient = autogen.UserProxyAgent(
            name="patient",
            human_input_mode="ALWAYS",
            max_consecutive_auto_reply=10,
            code_execution_config={"work_dir": "coding", "use_docker": False},
            llm_config=self.config_list,
            websocket=self.websocket
        )

        counselor = autogen.AssistantAgent(
            name="counselor",
            system_message=counselor_system_message,
            is_termination_msg=lambda x: self.check_termination(x),
            human_input_mode="NEVER",
            code_execution_config={"work_dir":"coding", "use_docker":False},
            llm_config=self.config_list,
            websocket=self.websocket
        )

        counselor_assistant = autogen.AssistantAgent(
            name="counselor_assistant",
            system_message=counselor_system_message,
            is_termination_msg=lambda x: self.check_termination(x),
            human_input_mode="NEVER",
            llm_config=self.config_list,
            websocket=self.websocket
        )

        self.agents = [counselor, patient, counselor_assistant]

        # Function definitions
        def answer_question_wrapper(user_question: str) -> str:
            return self.answer_question(user_question)
        
        async def assess_hiv_risk_wrapper() -> str:
            return await assess_hiv_risk(self.websocket)
        
        def search_provider_wrapper(zip_code: str) -> str:
            return search_provider(zip_code)

        async def assess_status_of_change_wrapper() -> str:
            return await assess_ttm_stage_single_question(self.websocket)
        
        async def record_support_request_wrapper() -> str:
            return await record_support_request(self.websocket, self.chat_id)
        

        # Register functions
        autogen.agentchat.register_function(
            record_support_request_wrapper,
            caller=counselor_assistant,
            executor=counselor,
            name="record_support_request",
            description="""Do not immediately call this function.
            Wait for the user to show signs of distress over time (DO NOT ACTIVATE THE FIRST TIME) or requests human support.
            For example, if the user suggests that they want 
            First ask if they are sure they want human support. 
            If they do, then call this function. If they don't, then do not call this function.""",
        )

        autogen.agentchat.register_function(
            assess_status_of_change_wrapper,
            caller=counselor_assistant,
            executor=counselor,
            name="assess_status_of_change",
            description="Assesses the status of change for the patient.",
        )

        autogen.agentchat.register_function(
            answer_question_wrapper,
            caller=counselor_assistant,
            executor=counselor,
            name="answer_question",
            description="""Use this function to get HIV/PrEP information by passing the user's question as a parameter.
        Example: answer_question("What are the side effects of PrEP?")
        REQUIRED: Must be called before providing ANY HIV/PrEP information.""",
        )

        autogen.agentchat.register_function(
            assess_hiv_risk_wrapper,
            caller=counselor_assistant,
            executor=counselor,
            name="assess_hiv_risk",
            description="Assesses HIV risk when the user explicitly asks for it.",
        )

        autogen.agentchat.register_function(
            search_provider_wrapper,
            caller=counselor_assistant,
            executor=counselor,
            name="search_provider",
            description="Returns a list of nearby providers. After getting the zip code, immediatelyb return the list of providers. DO NOT say anythiing such as: Please wait while I search for providers. Just return the list of providers.",
        )

        speaker_transitions = {
            counselor: [counselor_assistant, counselor],
            counselor_assistant: [counselor, counselor_assistant],
            patient: []
        }
        
        self.group_chat = autogen.GroupChat(
            agents=self.agents,
            messages=[],
            max_round=12,
            allowed_or_disallowed_speaker_transitions=speaker_transitions,
            speaker_transitions_type="disallowed"
        )

        self.manager = TrackableGroupChatManager(
            groupchat=self.group_chat, 
            llm_config=self.config_list,
            system_message="""Ensure counselor is primary responder. It should ALWAYS use FAQ agent's 
            knowledge for information unless the information is not available. Only use assessment_bot and search_bot for 
            explicit requests.
            
                1. Only one agent should respond to each user message
                2. After an agent responds, wait for the user's next message
                3. Never have multiple agents respond to the same user message,
                4. Ensure counselor responds first using FAQ agent's knowledge, 
                unless explicitly asked for risk assessment or provider search
                """,
            websocket=self.websocket
        )
        

        # Move teachability addition here and use the class instance
        if self.teachability_flag and self.teachability:
            self.teachability.add_to_agent(counselor)
            self.teachability.add_to_agent(counselor_assistant)

    def update_history(self, recipient, message, sender):
        self.agent_history.append({
            "sender": sender.name,
            "receiver": recipient.name,
            "message": message
        })
    
    def get_latest_response(self):
        """Get the latest valid response"""
        try:
            if not self.group_chat.messages:
                return None
                
            for message in reversed(self.group_chat.messages):
                if isinstance(message, dict) and message.get('content'):
                    return message['content']
                elif isinstance(message, str):
                    return message
                    
            return None
        except Exception as e:
            print(f"Error getting response: {e}")
            return None

    async def initiate_chat(self, user_input: str = None):
        if not user_input:
            return
            
        try:
            self.update_history(self.agents[2], user_input, self.agents[2])
            print("user_input", user_input)
            await self.agents[1].a_initiate_chat(
                recipient=self.manager,
                message=str(user_input),
                websocket=self.websocket,
                clear_history=False,
                system_message="""Ensure counselor responds first using FAQ agent's knowledge, 
                unless explicitly asked for risk assessment or provider search.  Ensure only one agent responds per turn. """
            )
        except Exception as e:
            print(f"Chat error: {e}")
            raise

    def get_history(self):
        return self.agent_history


    class Config:
        arbitrary_types_allowed = True

    def export_readable_db(user_id: str):
        db_dir = f"/Users/amaris/Desktop/AI_coder/counselling-chatbot/FastAPI/tmp/interactive/teachability_db/user_{user_id}"
        source_path = os.path.join(db_dir, "chroma.sqlite3")
        
        try:
            conn = sqlite3.connect(source_path)
            cursor = conn.cursor()
            
            print("\n=== Stored Memos ===")
            # Look in embedding_fulltext_search_content for actual content
            cursor.execute("""
                SELECT e.id, e.embedding_id, c.c0
                FROM embeddings e
                JOIN embedding_fulltext_search_content c ON e.embedding_id = c.id
                ORDER BY e.id DESC
                LIMIT 20
            """)
            
            rows = cursor.fetchall()
            for row in rows:
                print("\n--- Memo ---")
                print(f"ID: {row[0]}")
                print(f"Embedding ID: {row[1]}")
                print(f"Content: {row[2]}")
                print("-" * 50)
                
        except Exception as e:
            print(f"Error reading database: {e}")
            
        finally:
            if 'conn' in locals():
                conn.close()
    
        
    async def send_teachability_state(self):
        """Send current teachability state to frontend"""
        try:
            await self.websocket.send_json({
                "type": "teachability_flag",
                "content": self.teachability_flag
            })
        except Exception as e:
            print(f"Error sending teachability state: {e}")

    

    async def handle_websocket_message(self, message):
        """Handle incoming websocket messages"""
        try:
            data = json.loads(message)
            if data.get("type") == "teachability_flag":
                new_state = data.get("content")
                print(f"[UPDATE] Received teachability flag update: {new_state}")
                self.teachability_flag = new_state
                # Reinitialize agents with new teachability state
                self.initialize_agents()
                print(f"[CONFIRM] Teachability flag is now: {self.teachability_flag}")
                
                # Send confirmation back to frontend
                await self.websocket.send_json({
                    "type": "teachability_flag",
                    "content": self.teachability_flag
                })
        except Exception as e:
            print(f"Error handling websocket message: {e}")

# if __name__ == "__main__":
#     user_id = "3be0e3d8-f360-464c-ae52-8da66a5c5964"
#     print(f"Attempting to read database content for user: {user_id}")
#     export_readable_db(user_id)