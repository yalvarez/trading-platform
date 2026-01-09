import axios from "axios";

const API_URL = "http://localhost:8000/permisos";

export const getPermissions = async () => {
  const res = await axios.get(API_URL);
  return res.data;
};

export const createPermission = async (data) => {
  const res = await axios.post(API_URL, data);
  return res.data;
};

export const updatePermission = async (id, data) => {
  const res = await axios.put(`${API_URL}/${id}`, data);
  return res.data;
};

export const deletePermission = async (id) => {
  await axios.delete(`${API_URL}/${id}`);
};
