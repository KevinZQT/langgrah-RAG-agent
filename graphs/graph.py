
from langchain_core.runnables import RunnableConfig
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import BaseMessage, AIMessage
from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langgraph.graph import StateGraph, END
from .configuration import Configuration
from .state import OverallState
from .schema import SearchQuery
from .utils import get_message_text, get_current_date
from .prompts import query_generate_instructions, answer_instructions
from .retrieval import make_retriever


async def prepare_query(state: OverallState, config: RunnableConfig)-> dict[str, list[str]]:

    messages = state.messages
    if len(messages) == 1:
        query = get_message_text(messages[-1].content)
        return {"queries" : [query]}

    configuration = Configuration.from_runnable_config(config)
    prompt = ChatPromptTemplate.from_messages([
        ("system", query_generate_instructions),
        ("placeholder", "{conversation}")
    ])
    formatted_prompt = await prompt.ainvoke(
        {
            "queries": state.queries,
            "system_time": get_current_date(),
            "conversation": messages
        },
        config
    )
    model = init_chat_model(configuration.query_generate_model, timeout=45, max_retries=1).with_structured_output(SearchQuery)
    result = await model.ainvoke(formatted_prompt,config)
    return {
        "queries": [result.query]
    }


async def retrieve(state: OverallState, config: RunnableConfig)-> dict[str, list[Document]]:
    configuration = Configuration.from_runnable_config(config)
    with make_retriever(configuration) as retriever:

        result = await retriever.ainvoke(state.queries[-1], config)

        return {
            "retrieved_docs": result
        }


async def answer(state: OverallState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:

    if not state.retrieved_docs:
        return {"messages": [AIMessage(content="No relevant content was found in the selected documents. Rephrase your question or add more sources.")]} 

    configuration = Configuration.from_runnable_config(config)

    prompt = ChatPromptTemplate.from_messages([
        ("system", answer_instructions),
        ("placeholder", "{conversation}")
    ])

    formatted_prompt = await prompt.ainvoke(
        {
            "retrieved_docs": "\n\n".join(
                f"[{i}] Source: {doc.metadata.get('source', 'Unknown source')}; page: {doc.metadata.get('page', 'not available')}\n{doc.page_content}"
                for i, doc in enumerate(state.retrieved_docs, 1)
            ),
            "system_time": get_current_date(),
            "conversation": state.messages
        },
        config
    )

    model = init_chat_model(configuration.answer_model, timeout=45, max_retries=1)

    result = await model.ainvoke(formatted_prompt, config)

    return {
        "messages": [result]
    }


builder = StateGraph(OverallState)

builder.add_node(prepare_query)
builder.add_node(retrieve)
builder.add_node(answer)

builder.set_entry_point("prepare_query")
builder.add_edge("prepare_query", "retrieve")
builder.add_edge("retrieve", "answer")
builder.add_edge("answer", END)

graph = builder.compile()

graph.name = "RetrievalGraph"

