import warnings

# Suppress Pydantic UserWarning about json attribute shadowing BaseModel.json method
warnings.filterwarnings("ignore", message='.*"json".*"BaseModel".*', category=UserWarning, module="pydantic")

# Suppress Pydantic UserWarning about json attribute shadowing BaseModel.schema method
warnings.filterwarnings("ignore", message='.*"schema".*"BaseModel".*', category=UserWarning, module="pydantic")
